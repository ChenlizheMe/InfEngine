"""Private import table for runtime script reload candidates.

Candidate scripts are ordinary Python modules, but they must not be executed
through the process-wide import table.  This module provides a small,
transaction-scoped importer instead.  Project modules are loaded into a
private table and may import one another, including cycles.  Imports outside
that table are admitted only from an explicit, already trusted module set.

This is an isolation boundary for reload correctness, not a security sandbox:
the static candidate policy remains responsible for rejecting side effects.
"""

from __future__ import annotations

import builtins
import importlib
import importlib.util
import os
import sys
import tokenize
import types
from dataclasses import dataclass
from typing import Iterable

from .path_utils import path_key, relative_path, resolved_path
from .project_context import get_assets_root, get_project_root, get_script_import_paths


class CandidateImportError(ImportError):
    """Raised when a candidate import cannot be proven to be controlled."""


_TRUSTED_MODULE_PREFIXES = frozenset(
    {
        "Infernux",
        "__future__",
        "dataclasses",
        "typing",
        "math",
        "enum",
        "os",
        "pathlib",
        "collections",
        "collections.abc",
        "copy",
        "functools",
        "itertools",
        "operator",
        "random",
        "numpy",
    }
)

# These public engine modules are safe to materialize on first candidate use.
# Keep this list deliberately narrow: general ``Infernux.*`` imports must not
# turn candidate loading into an uncontrolled engine-module import mechanism.
_LAZY_TRUSTED_MODULES = frozenset({"Infernux.jit", "__future__"})


def _is_trusted_module(name: str) -> bool:
    return any(
        name == prefix or name.startswith(prefix + ".")
        for prefix in _TRUSTED_MODULE_PREFIXES
    )


def _module_names_from_path(path: str, roots: Iterable[str]) -> tuple[str, ...]:
    normalized = resolved_path(path)
    names: list[str] = []
    for root in roots:
        try:
            relative = relative_path(normalized, root)
        except ValueError:
            continue
        stem, extension = os.path.splitext(relative)
        if extension not in {".py", ".pyc"}:
            continue
        parts = stem.replace("\\", "/").split("/")
        if parts and parts[-1] == "__init__":
            parts.pop()
        if parts and all(part.isidentifier() for part in parts):
            name = ".".join(parts)
            if name not in names:
                names.append(name)
    return tuple(names)


@dataclass(frozen=True, slots=True)
class CandidateModuleSpec:
    name: str
    file_path: str
    source: bytes | str | None = None
    code: types.CodeType | None = None
    namespace: bool = False


class CandidateImportTransaction:
    """Execute a candidate dependency closure without publishing modules.

    ``load`` inserts an empty module into ``modules`` before executing it, so
    normal circular imports observe the same object.  The module table is
    never exposed through ``sys.modules`` until ``commit`` is called.
    """

    def __init__(self, *, trusted_modules: Iterable[str] = ()) -> None:
        self._specs: dict[str, CandidateModuleSpec] = {}
        self._modules: dict[str, types.ModuleType] = {}
        self._reused_lkg: dict[str, types.ModuleType] = {}
        self._roots: list[str] = []
        self._trusted_modules = frozenset(trusted_modules)
        self._trusted_proxies: dict[str, types.ModuleType] = {}
        self._overlay_names: set[str] = set()
        self._parent_before: list[tuple[types.ModuleType, str, bool, object]] = []
        self._parent_before_keys: set[tuple[int, str]] = set()
        self._before: dict[str, object] = {}
        self._committed = False
        self._rolled_back = False

    @property
    def modules(self) -> dict[str, types.ModuleType]:
        """Return the private candidate module table."""
        return dict(self._modules)

    @property
    def publishable_modules(self) -> tuple[types.ModuleType, ...]:
        """Return candidate modules, excluding shallow live-package overlays."""
        return tuple(
            module
            for name, module in self._modules.items()
            if name not in self._overlay_names
        )

    @property
    def loaded_module_names(self) -> tuple[str, ...]:
        return tuple(self._modules)

    def register(
        self,
        name: str,
        file_path: str,
        *,
        source: bytes | str | None = None,
        code: types.CodeType | None = None,
    ) -> None:
        if not name or not isinstance(name, str):
            raise ValueError("candidate module name must be non-empty")
        if name in self._specs:
            raise CandidateImportError(f"candidate module registered twice: {name}")
        path = resolved_path(file_path)
        if not path or not os.path.isfile(path):
            raise CandidateImportError(f"candidate module file not found: {file_path}")
        self._specs[name] = CandidateModuleSpec(name, path, source, code)
        for root in get_script_import_paths(path):
            root = resolved_path(root)
            if root and root not in self._roots:
                self._roots.append(root)

    def module_for(self, name: str) -> types.ModuleType | None:
        return self._modules.get(name)

    def load(self, name: str) -> types.ModuleType:
        if self._rolled_back:
            raise CandidateImportError("candidate import transaction has been rolled back")
        if name in self._modules:
            return self._modules[name]
        spec = self._specs.get(name)
        if spec is None:
            lkg = self._reuse_project_lkg(name)
            if lkg is not None:
                if self._has_registered_descendant(name):
                    return self._project_package_overlay(name, lkg)
                return lkg
            # Engine and standard-library imports are already admitted by the
            # candidate policy. Resolve them before searching every live
            # project module for a possible package descendant; ordinary
            # scripts import Infernux several times and the old ordering made
            # each import walk and normalize the whole sys.modules table.
            if _is_trusted_module(name) or name in self._trusted_modules:
                return self._load_trusted(name)
            if self._has_registered_descendant(name):
                return self._create_namespace(name)
            if self._has_lkg_descendant(name):
                return self._create_namespace(name)
            raise CandidateImportError(
                f"candidate import rejected: project dependency '{name}' is not registered in this transaction "
                "and has no valid preloaded LKG module"
            )
        return self._execute(spec)

    def _has_registered_descendant(self, name: str) -> bool:
        prefix = name + "."
        return any(candidate.startswith(prefix) for candidate in self._specs)

    def _has_lkg_descendant(self, name: str) -> bool:
        prefix = name + "."
        return any(
            candidate.startswith(prefix)
            and self._reuse_project_lkg(candidate) is not None
            for candidate in tuple(sys.modules)
        )

    def _create_namespace(self, name: str) -> types.ModuleType:
        if name in self._modules:
            return self._modules[name]
        child_paths = [
            os.path.dirname(spec.file_path)
            for child_name, spec in self._specs.items()
            if child_name.startswith(name + ".") and spec.file_path
        ]
        child_paths.extend(
            os.path.dirname(resolved_path(getattr(module, "__file__", "")))
            for child_name, module in sys.modules.items()
            if child_name.startswith(name + ".")
            and self._reuse_project_lkg(child_name) is not None
            and getattr(module, "__file__", "")
        )
        if not child_paths:
            raise CandidateImportError(f"candidate namespace is not registered: '{name}'")
        module = types.ModuleType(name)
        module.__path__ = [resolved_path(child_paths[0])]
        module.__package__ = name
        module.__file__ = None
        module.__spec__ = None
        module.__builtins__ = self._builtins_for(module)
        self._modules[name] = module
        self._attach_child(name, module)
        return module

    def _project_package_overlay(
        self,
        name: str,
        module: types.ModuleType,
    ) -> types.ModuleType:
        """Return a private package view for candidate child attachment.

        Reusing a project's last-known-good package object directly is safe for
        reads, but attaching a candidate child to that live object before the
        owner commits leaks staged state to the running game.  The shallow
        overlay preserves the package API while keeping child attributes in the
        transaction-private module table.
        """
        cached = self._modules.get(name)
        if cached is not None:
            return cached
        if not hasattr(module, "__path__"):
            return module
        overlay = types.ModuleType(name)
        overlay.__dict__.update(vars(module))
        self._modules[name] = overlay
        self._overlay_names.add(name)
        self._attach_child(name, overlay)
        return overlay

    def _reuse_project_lkg(self, name: str) -> types.ModuleType | None:
        if name in self._reused_lkg:
            return self._reused_lkg[name]
        module = sys.modules.get(name)
        if module is None:
            return None
        module_path = resolved_path(getattr(module, "__file__", "") or "")
        if not module_path:
            return None
        module_key = path_key(module_path)
        project_roots = tuple(path_key(root) for root in self._roots if root)
        if not any(
            module_key == root
            or module_key.startswith(root.rstrip("\\/") + "\\")
            for root in project_roots
        ):
            # Trusted interpreter/engine modules are not project LKG entries.
            return None
        expected = _module_names_from_path(module_path, self._roots)
        if name not in expected:
            raise CandidateImportError(
                f"preloaded project module '{name}' has a path/name mismatch"
            )
        self._reused_lkg[name] = module
        return module

    def _load_trusted(self, name: str):
        if not (_is_trusted_module(name) or name in self._trusted_modules):
            raise CandidateImportError(
                f"candidate import rejected: '{name}' is not a project module or trusted preloaded module"
            )
        module = sys.modules.get(name)
        if module is None and name in _LAZY_TRUSTED_MODULES:
            module = importlib.import_module(name)
        if module is None:
            raise CandidateImportError(
                f"trusted candidate import is not preloaded: '{name}'"
            )
        if name == "dataclasses":
            return self._dataclasses_proxy(module)
        return module

    def _dataclasses_proxy(self, module: types.ModuleType) -> types.ModuleType:
        cached = self._trusted_proxies.get("dataclasses")
        if cached is not None:
            return cached
        proxy = types.ModuleType("dataclasses")
        proxy.__dict__.update(vars(module))
        real_dataclass = module.dataclass

        def candidate_dataclass(cls=None, /, **kwargs):
            def decorate(target):
                annotations = getattr(target, "__annotations__", None)
                original = dict(annotations) if isinstance(annotations, dict) else None
                original_module = getattr(target, "__module__", None)
                if annotations is not None:
                    for key, value in tuple(annotations.items()):
                        if not isinstance(value, str):
                            continue
                        try:
                            resolved = value
                            for _ in range(2):
                                if not isinstance(resolved, str):
                                    break
                                resolved = eval(
                                    resolved,
                                    target.__dict__,
                                    target.__dict__,
                                )
                            annotations[key] = resolved
                        except Exception:
                            pass
                if any(isinstance(value, str) for value in (annotations or {}).values()):
                    # dataclasses uses sys.modules for a few string-annotation
                    # checks. Point that lookup at an existing trusted module,
                    # never at the private candidate module.
                    target.__module__ = "builtins"
                try:
                    return real_dataclass(target, **kwargs)
                finally:
                    if original_module is not None:
                        target.__module__ = original_module
                    if original is not None:
                        target.__annotations__ = original

            return decorate if cls is None else decorate(cls)

        proxy.dataclass = candidate_dataclass
        self._trusted_proxies["dataclasses"] = proxy
        return proxy

    def _execute(self, spec: CandidateModuleSpec) -> types.ModuleType:
        if spec.name in self._modules:
            return self._modules[spec.name]
        if spec.namespace:
            module = types.ModuleType(spec.name)
            module.__file__ = None
            module.__path__ = [spec.file_path]
            module.__package__ = spec.name
            module.__spec__ = None
            self._modules[spec.name] = module
            return module
        import_spec = importlib.util.spec_from_file_location(spec.name, spec.file_path)
        if import_spec is None or import_spec.loader is None:
            raise CandidateImportError(f"failed to create candidate module spec: {spec.file_path}")
        if spec.file_path.endswith((os.sep + "__init__.py", "/__init__.py", "\\__init__.py")):
            import_spec.submodule_search_locations = [os.path.dirname(spec.file_path)]
        module = importlib.util.module_from_spec(import_spec)
        self._modules[spec.name] = module
        module.__builtins__ = self._builtins_for(module)
        try:
            code = spec.code
            if code is None and spec.source is not None:
                if not spec.file_path.endswith(".py"):
                    raise CandidateImportError("source candidate must point to a .py file")
                if isinstance(spec.source, bytes):
                    source = spec.source.decode("utf-8")
                else:
                    source = spec.source
                code = compile(source, spec.file_path, "exec", dont_inherit=True)
            elif code is None and spec.file_path.endswith(".py"):
                # A reload candidate must read the current source even when
                # the frontend did not provide a snapshot; never reuse a
                # timestamp/size-matching stale pyc during staging.
                with tokenize.open(spec.file_path) as source_file:
                    source = source_file.read()
                code = compile(source, spec.file_path, "exec", dont_inherit=True)
            if code is not None:
                exec(code, module.__dict__)
            else:
                loader = import_spec.loader
                get_code = getattr(loader, "get_code", None)
                if not callable(get_code):
                    raise CandidateImportError(
                        f"candidate loader does not expose code: {spec.file_path}"
                    )
                loaded_code = get_code(spec.name)
                if loaded_code is None:
                    raise CandidateImportError(f"candidate loader returned no code: {spec.file_path}")
                exec(loaded_code, module.__dict__)
        except Exception:
            self._modules.pop(spec.name, None)
            raise
        self._attach_child(spec.name, module)
        return module

    def _attach_child(self, name: str, module: types.ModuleType) -> None:
        parent_name, _, child_name = name.rpartition(".")
        if not parent_name:
            return
        parent = self._modules.get(parent_name)
        if parent is None:
            live_parent = self._reuse_project_lkg(parent_name)
            if live_parent is not None:
                parent = self._project_package_overlay(parent_name, live_parent)
        if parent is not None:
            setattr(parent, child_name, module)

    def _try_load_child(self, parent_name: str, child_name: str) -> None:
        child = f"{parent_name}.{child_name}"
        if child in self._modules:
            return
        child_spec = self._specs.get(child)
        if child_spec is not None:
            self.load(child)
            return
        if self._reuse_project_lkg(child) is not None:
            return

    def _builtins_for(self, module: types.ModuleType) -> dict[str, object]:
        values = dict(vars(builtins))
        values["__import__"] = self._import
        return values

    def _import(
        self,
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] | list[str] = (),
        level: int = 0,
    ):
        del locals
        package = str((globals or {}).get("__package__") or "")
        try:
            relative_name = ("." * level) + name if level else name
            absolute = importlib.util.resolve_name(relative_name, package) if level else name
        except (ImportError, ValueError) as exc:
            raise CandidateImportError(f"invalid relative candidate import '{name}'") from exc
        module = self.load(absolute)
        if fromlist:
            for item in fromlist:
                if item == "*":
                    continue
                child = f"{absolute}.{item}"
                # A registered candidate child wins over an attribute copied
                # from the live package into its private overlay.  Otherwise
                # ``from package import child`` could silently retain the LKG
                # child and bypass this transaction's dependency closure.
                if child in self._specs or child in self._modules:
                    self._try_load_child(absolute, item)
                elif not hasattr(module, item):
                    self._try_load_child(absolute, item)
            return self._modules.get(absolute, module)
        root_name = absolute.split(".", 1)[0]
        if root_name == absolute:
            return module
        root = self.load(root_name)
        self._attach_child(absolute, module)
        return root

    def commit(self) -> None:
        if self._rolled_back:
            raise CandidateImportError("candidate import transaction has been rolled back")
        if self._committed:
            return
        try:
            from Infernux.engine.runtime_dispatch import assert_runtime_dispatch_safe_point

            assert_runtime_dispatch_safe_point()
            published_names = tuple(
                name for name in self._modules if name not in self._overlay_names
            )
            published_set = set(published_names)
            for name in published_names:
                module = self._modules[name]
                self._before.setdefault(name, sys.modules.get(name, _MODULE_ABSENT))
                sys.modules[name] = module
            # A private overlay is never published as a replacement package.
            # Once every child module is visible, update its live parent as the
            # final package-level side effect and retain an exact before-image.
            for name in published_names:
                parent_name, _, child_name = name.rpartition(".")
                if not parent_name or parent_name in published_set:
                    continue
                parent = sys.modules.get(parent_name)
                if parent is None:
                    continue
                key = (id(parent), child_name)
                if key not in self._parent_before_keys:
                    values = vars(parent)
                    self._parent_before.append(
                        (parent, child_name, child_name in values, values.get(child_name))
                    )
                    self._parent_before_keys.add(key)
                setattr(parent, child_name, self._modules[name])
            self._committed = True
        except Exception:
            self.rollback()
            raise

    def rollback(self) -> None:
        if self._rolled_back:
            return
        if self._committed:
            from Infernux.engine.runtime_dispatch import assert_runtime_dispatch_safe_point

            assert_runtime_dispatch_safe_point()
        for parent, child_name, present, previous in reversed(self._parent_before):
            if present:
                setattr(parent, child_name, previous)
            else:
                try:
                    delattr(parent, child_name)
                except AttributeError:
                    pass
        for name, previous in self._before.items():
            if previous is _MODULE_ABSENT:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        self._modules.clear()
        self._overlay_names.clear()
        self._parent_before.clear()
        self._parent_before_keys.clear()
        self._rolled_back = True
        self._committed = False


_MODULE_ABSENT = object()


__all__ = ["CandidateImportError", "CandidateImportTransaction", "CandidateModuleSpec"]
