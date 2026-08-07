"""Global command authority for editor preferences."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Optional


SUPPORTED_LOCALES = frozenset({"en", "zh"})
SUPPORTED_IDES = frozenset({"vscode", "pycharm"})


class PreferencesCommandService:
    """Apply persistent editor preferences through non-dirty undo commands."""

    _instance: Optional["PreferencesCommandService"] = None

    def __init__(self) -> None:
        self._shortcut_profiles = None
        self._shortcut_router = None
        PreferencesCommandService._instance = self

    @classmethod
    def instance(cls) -> Optional["PreferencesCommandService"]:
        return cls._instance

    def shutdown(self) -> None:
        self._shortcut_profiles = None
        self._shortcut_router = None
        if PreferencesCommandService._instance is self:
            PreferencesCommandService._instance = None

    @property
    def shortcut_profiles(self):
        return self._shortcut_profiles

    def bind_shortcuts(
        self,
        default_bindings: Iterable[Any],
        router: Any,
        *,
        load: Callable[[], object] | None = None,
        save: Callable[[dict], None] | None = None,
    ):
        """Install the one profile model which projects effective Router bindings."""

        from .shortcut_profiles import ShortcutProfileModel
        from .shortcuts import ShortcutBinding, ShortcutRouter

        defaults = tuple(default_bindings)
        if any(not isinstance(binding, ShortcutBinding) for binding in defaults):
            raise TypeError("shortcut defaults must contain ShortcutBinding values")
        if not isinstance(router, ShortcutRouter):
            raise TypeError("shortcut profiles require the shared ShortcutRouter")

        if load is None or save is None:
            from Infernux.engine.preferences_store import PreferencesStore

            store = PreferencesStore()
            load = load or (lambda: store.get("shortcut_profiles", None))
            save = save or (lambda payload: store.set("shortcut_profiles", payload))

        try:
            model = ShortcutProfileModel(defaults, load=load, save=save)
        except (KeyError, TypeError, ValueError) as exc:
            # This project is still in a destructive schema phase. An old or
            # foreign shortcut document is discarded instead of keeping a
            # half-applicable profile alive beside the current command table.
            from Infernux.debug import Debug

            Debug.log_warning(
                f"Discarding incompatible shortcut preferences: {exc}"
            )
            model = ShortcutProfileModel(defaults, save=save)
            save(model.to_json_data())

        self._shortcut_profiles = model
        self._shortcut_router = router
        self._publish_effective_shortcuts()
        return model

    def register_commands(self, registry: Any) -> None:
        from .commands import EditorCommand

        registry.register(
            EditorCommand(
                "preferences.set_locale",
                lambda context: self.set_locale(context.payload.get("value", "")),
                display_name="Set Editor Language",
                category="Preferences",
                can_execute=lambda context: str(
                    context.payload.get("value", "") or ""
                ).strip()
                in SUPPORTED_LOCALES,
            )
        )
        registry.register(
            EditorCommand(
                "preferences.shortcut.create_profile",
                lambda context: self.create_shortcut_profile(
                    context.payload.get("name", ""),
                    profile_id=context.payload.get("profile_id", None),
                ),
                display_name="Create Shortcut Profile",
                category="Preferences/Shortcuts",
                can_execute=lambda context: bool(
                    self._shortcut_profiles is not None
                    and str(context.payload.get("name", "") or "").strip()
                ),
            )
        )
        registry.register(
            EditorCommand(
                "preferences.shortcut.rename_profile",
                lambda context: self.rename_shortcut_profile(
                    context.payload.get("profile_id", ""),
                    context.payload.get("name", ""),
                ),
                display_name="Rename Shortcut Profile",
                category="Preferences/Shortcuts",
                can_execute=lambda context: self._has_user_shortcut_profile(
                    context.payload.get("profile_id", "")
                )
                and bool(str(context.payload.get("name", "") or "").strip()),
            )
        )
        registry.register(
            EditorCommand(
                "preferences.shortcut.delete_profile",
                lambda context: self.delete_shortcut_profile(
                    context.payload.get("profile_id", "")
                ),
                display_name="Delete Shortcut Profile",
                category="Preferences/Shortcuts",
                can_execute=lambda context: self._has_user_shortcut_profile(
                    context.payload.get("profile_id", "")
                ),
            )
        )
        registry.register(
            EditorCommand(
                "preferences.shortcut.activate_profile",
                lambda context: self.activate_shortcut_profile(
                    context.payload.get("profile_id", "")
                ),
                display_name="Activate Shortcut Profile",
                category="Preferences/Shortcuts",
                can_execute=lambda context: self._has_shortcut_profile(
                    context.payload.get("profile_id", "")
                ),
            )
        )
        registry.register(
            EditorCommand(
                "preferences.shortcut.assign",
                lambda context: self.assign_shortcut(
                    context.payload.get("binding_id", ""),
                    None
                    if bool(context.payload.get("disabled", False))
                    else context.payload.get("chord", ""),
                ),
                display_name="Assign Shortcut",
                category="Preferences/Shortcuts",
                can_execute=lambda context: self._can_edit_shortcut_binding(
                    context.payload.get("binding_id", "")
                )
                and (
                    bool(context.payload.get("disabled", False))
                    or bool(str(context.payload.get("chord", "") or "").strip())
                ),
            )
        )
        registry.register(
            EditorCommand(
                "preferences.shortcut.reset_binding",
                lambda context: self.reset_shortcut_binding(
                    context.payload.get("binding_id", "")
                ),
                display_name="Reset Shortcut",
                category="Preferences/Shortcuts",
                can_execute=lambda context: self._can_edit_shortcut_binding(
                    context.payload.get("binding_id", "")
                ),
            )
        )
        registry.register(
            EditorCommand(
                "preferences.shortcut.reset_profile",
                lambda _context: self.reset_shortcut_profile(),
                display_name="Reset Shortcut Profile",
                category="Preferences/Shortcuts",
                can_execute=lambda _context: self._has_user_shortcut_profile(
                    self._active_shortcut_profile_id()
                ),
            )
        )
        registry.register(
            EditorCommand(
                "preferences.set_ide",
                lambda context: self.set_ide(context.payload.get("value", "")),
                display_name="Set Preferred IDE",
                category="Preferences",
                can_execute=lambda context: str(
                    context.payload.get("value", "") or ""
                ).strip()
                in SUPPORTED_IDES,
            )
        )

    def set_locale(self, locale: object) -> bool:
        from Infernux.engine.i18n import get_locale, set_locale

        value = str(locale or "").strip()
        if value not in SUPPORTED_LOCALES:
            return False
        return self._set_value(
            get_locale(),
            value,
            set_locale,
            description="Set Editor Language",
        )

    def set_ide(self, ide: object) -> bool:
        from Infernux.engine.ide_preference import get_ide, set_ide

        value = str(ide or "").strip()
        if value not in SUPPORTED_IDES:
            return False
        return self._set_value(
            get_ide(),
            value,
            set_ide,
            description="Set Preferred IDE",
        )

    def create_shortcut_profile(self, name: object, *, profile_id=None) -> bool:
        model = self._require_shortcut_profiles()
        diff = model.create_profile(str(name or ""), profile_id=profile_id)
        return self._record_shortcut_diff(diff, "Create Shortcut Profile")

    def rename_shortcut_profile(self, profile_id: object, name: object) -> bool:
        model = self._require_shortcut_profiles()
        diff = model.rename_profile(str(profile_id or ""), str(name or ""))
        return self._record_shortcut_diff(diff, "Rename Shortcut Profile")

    def delete_shortcut_profile(self, profile_id: object) -> bool:
        model = self._require_shortcut_profiles()
        diff = model.delete_profile(str(profile_id or ""))
        return self._record_shortcut_diff(diff, "Delete Shortcut Profile")

    def activate_shortcut_profile(self, profile_id: object) -> bool:
        model = self._require_shortcut_profiles()
        diff = model.set_active_profile(str(profile_id or ""))
        return self._record_shortcut_diff(diff, "Activate Shortcut Profile")

    def assign_shortcut(self, binding_id: object, chord: object) -> bool:
        model = self._require_shortcut_profiles()
        identity = str(binding_id or "").strip()
        conflicts = model.conflicts_for(identity, chord)
        if conflicts:
            conflict_ids = ", ".join(binding.binding_id for binding in conflicts)
            raise ValueError(f"shortcut conflicts with: {conflict_ids}")
        diff = model.assign(identity, chord)
        return self._record_shortcut_diff(diff, "Assign Shortcut")

    def reset_shortcut_binding(self, binding_id: object) -> bool:
        model = self._require_shortcut_profiles()
        diff = model.reset_binding(str(binding_id or ""))
        return self._record_shortcut_diff(diff, "Reset Shortcut")

    def reset_shortcut_profile(self) -> bool:
        model = self._require_shortcut_profiles()
        diff = model.reset_profile()
        return self._record_shortcut_diff(diff, "Reset Shortcut Profile")

    def _record_shortcut_diff(self, diff, description: str) -> bool:
        if diff is None:
            return False
        self._publish_effective_shortcuts()

        from Infernux.engine.undo import LambdaCommand, UndoManager

        manager = UndoManager.instance()
        if manager is None or not manager.enabled or manager.is_executing:
            return True
        command = LambdaCommand(
            description,
            undo_fn=lambda: self._restore_shortcut_snapshot(diff.before),
            redo_fn=lambda: self._restore_shortcut_snapshot(diff.after),
            marks_dirty=False,
        )
        if manager.record(command):
            return True
        self._restore_shortcut_snapshot(diff.before)
        return False

    def _restore_shortcut_snapshot(self, snapshot) -> None:
        self._require_shortcut_profiles().restore_snapshot(snapshot)
        self._publish_effective_shortcuts()

    def _publish_effective_shortcuts(self) -> None:
        model = self._shortcut_profiles
        router = self._shortcut_router
        if model is None or router is None:
            return
        router.clear()
        for binding in model.effective_bindings():
            router.register(binding)

    def _require_shortcut_profiles(self):
        if self._shortcut_profiles is None:
            raise RuntimeError("shortcut profiles are not bound to editor defaults")
        return self._shortcut_profiles

    def _active_shortcut_profile_id(self) -> str:
        model = self._shortcut_profiles
        return "" if model is None else model.active_profile_id

    def _has_shortcut_profile(self, profile_id: object) -> bool:
        model = self._shortcut_profiles
        if model is None:
            return False
        try:
            model.profile(str(profile_id or ""))
            return True
        except (KeyError, TypeError, ValueError):
            return False

    def _has_user_shortcut_profile(self, profile_id: object) -> bool:
        from .shortcut_profiles import DEFAULT_PROFILE_ID

        identity = str(profile_id or "").strip()
        return identity != DEFAULT_PROFILE_ID and self._has_shortcut_profile(identity)

    def _can_edit_shortcut_binding(self, binding_id: object) -> bool:
        model = self._shortcut_profiles
        if model is None or not self._has_user_shortcut_profile(model.active_profile_id):
            return False
        try:
            model.binding(str(binding_id or ""))
            return True
        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _set_value(old_value, new_value, apply, *, description: str) -> bool:
        if old_value == new_value:
            return False
        from .view_commands import ViewCommandService

        if ViewCommandService.require().set_value(
            old_value,
            new_value,
            apply,
            description=description,
        ):
            return True
        apply(new_value)
        return True


def submit_preferences_command(command_id: str, *, source=None, value: object) -> bool:
    """Submit one Preferences intent through the global command registry."""
    from .commands import CommandSource
    from .session import EditorInteractionCore

    core = EditorInteractionCore.instance()
    if core is None:
        raise RuntimeError("Preferences edit requires the editor interaction core")
    result = core.commands.execute(
        command_id,
        source=CommandSource.API if source is None else CommandSource(source),
        payload={"value": value},
    )
    if not result.accepted:
        raise RuntimeError(
            result.message or f"Preferences command was rejected: {command_id}"
        )
    return True


__all__ = [
    "PreferencesCommandService",
    "SUPPORTED_IDES",
    "SUPPORTED_LOCALES",
    "submit_preferences_command",
]
