"""Hot-reload support for RenderStack pipeline definitions."""
from __future__ import annotations

import sys
import warnings


class PipelineReloadMixin:
    """PipelineReloadMixin method group for RenderStack."""

    def _register_pipeline_reload(self, pipeline_cls) -> None:
        """Subscribe to watchdog file-change events for the pipeline's source file."""
        import sys as _sys
        mod = _sys.modules.get(pipeline_cls.__module__)
        if mod is None:
            return
        src = getattr(mod, '__file__', None)
        if not src:
            return
        self._pipeline_module = mod
        from Infernux.engine.resources_manager import ResourcesManager
        rm = ResourcesManager.instance()
        if rm is not None:
            rm.register_script_reload_callback(src, self._on_pipeline_file_changed)

    def _unregister_pipeline_reload(self) -> None:
        """Unsubscribe from watchdog callbacks."""
        from Infernux.engine.resources_manager import ResourcesManager
        rm = ResourcesManager.instance()
        if rm is not None:
            rm.unregister_script_reload_callback(self._on_pipeline_file_changed)
        self._pipeline_module = None

    def _on_pipeline_file_changed(self, file_path: str) -> None:
        """Watchdog callback — called on main thread when pipeline source is saved."""
        import importlib
        from Infernux.renderstack.discovery import invalidate_discovery_cache
        mod = self._pipeline_module
        if mod is None:
            return
        print("[RenderStack] Pipeline file changed, reloading...", file=sys.stderr)
        self._save_current_pipeline_params()
        invalidate_discovery_cache()
        importlib.reload(mod)
        self._pipeline = None   # re-instantiate on next .pipeline access
        self.invalidate_graph() # clears _build_failed + _graph_desc
        print("[RenderStack] Pipeline reloaded.", file=sys.stderr)

    def _sync_pipeline_catalog(self) -> None:
        """Refresh available pipeline catalog and enforce fallback policy."""
        names = set(self.discover_pipelines().keys())
        signature = tuple(sorted(names))
        if signature == self._pipeline_catalog_signature:
            return

        self._pipeline_catalog_signature = signature

        current = self.pipeline_class_name
        if current and current not in names:
            warnings.warn(
                f"[RenderStack] Pipeline '{current}' was removed. Falling back to DefaultForwardPipeline.",
                stacklevel=2,
            )
            self.set_pipeline("")
            return

        # Refresh pipeline type on catalog changes so newly edited classes can be re-instantiated.
        if self._pipeline is not None:
            self._save_current_pipeline_params()
            self._pipeline = None
            self._cached_ips = None
            self.invalidate_graph()

    def _register_pipeline_catalog_reload(self) -> None:
        """Subscribe to watchdog-driven script catalog changes."""
        from Infernux.engine.resources_manager import ResourcesManager
        rm = ResourcesManager.instance()
        if rm is not None:
            rm.register_script_catalog_callback(self._on_script_catalog_changed)

    def _unregister_pipeline_catalog_reload(self) -> None:
        """Unsubscribe from watchdog-driven script catalog changes."""
        from Infernux.engine.resources_manager import ResourcesManager
        rm = ResourcesManager.instance()
        if rm is not None:
            rm.unregister_script_catalog_callback(self._on_script_catalog_changed)

    def _on_script_catalog_changed(self, file_path: str, event_type: str) -> None:
        """ResourcesManager callback for create/delete/move/modify of python scripts."""
        from Infernux.renderstack.discovery import invalidate_discovery_cache
        invalidate_discovery_cache()
        self._sync_pipeline_catalog()

