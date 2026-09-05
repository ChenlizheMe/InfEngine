"""ShaderInfo queries and canonical authoring reloads, without a second cache."""

from __future__ import annotations

from pathlib import Path


class Shader:
    """Query published shaders and reimport edited ShaderInfo source assets."""

    @staticmethod
    def _validate_shader_type(shader_type: str) -> str:
        normalized = str(shader_type).strip().lower()
        if normalized not in {"vertex", "fragment"}:
            raise ValueError(
                "shader_type must be 'vertex' or 'fragment'; "
                "compute shaders are not supported (use an external parallel backend)"
            )
        return normalized

    @classmethod
    def is_loaded(cls, name: str, shader_type: str = "vertex") -> bool:
        """Query GPU publication by ShaderInfo Name and stage, without loading.

        Returns False before startup, after shutdown, or without a renderer.
        Both standalone stages and linked material programs are included.
        This does not claim the last source edit compiled successfully.
        """
        from Infernux.core.assets import AssetManager

        stage = cls._validate_shader_type(shader_type)
        native = AssetManager._native_engine()
        return native is not None and native.is_shader_loaded(str(name), stage)

    @classmethod
    def reload(cls, shader_id: str, shader_type: str | None = None) -> bool:
        """Reimport a ShaderInfo Name or registered .vert/.frag asset path.

        Editor/headless authoring only; frozen Player assets are read-only.
        A Name shared by vertex and fragment stages reloads both unless a stage
        is specified. Duplicate Names within a stage require an explicit path.
        Returns True after every selected reimport succeeds; import/compiler
        errors raise RuntimeError instead of silently claiming success.
        """
        from Infernux.application import Application
        from Infernux.core.assets import AssetManager

        stage = cls._validate_shader_type(shader_type) if shader_type is not None else None
        if Application.is_player():
            raise RuntimeError("Shader.reload requires authoring assets; frozen Player shaders are read-only")
        database = AssetManager.require_asset_database()
        selector = str(shader_id)
        candidate = Path(selector)
        if not candidate.is_absolute():
            candidate = Path(database.project_root) / candidate
        direct_guid = database.get_guid_from_path(str(candidate))
        paths = []
        if direct_guid:
            path = database.get_path_from_guid(direct_guid)
            path_stage = {".vert": "vertex", ".frag": "fragment"}.get(Path(path).suffix.lower())
            if path_stage is None or (stage is not None and path_stage != stage):
                raise ValueError(f"Not a matching ShaderInfo stage asset: {selector}")
            paths.append(path)
        else:
            by_stage = {}
            for guid in database.get_all_guids():
                meta = database.get_meta_by_guid(guid)
                if meta is None or not meta.has_key("shader_id") or not meta.has_key("type"):
                    continue
                current_stage = meta.get_string("type")
                if current_stage not in {"vertex", "fragment"} or (stage is not None and current_stage != stage):
                    continue
                if meta.get_string("shader_id") != selector:
                    continue
                if current_stage in by_stage:
                    raise ValueError(f"Ambiguous ShaderInfo Name {selector!r}; use an asset path")
                by_stage[current_stage] = database.get_path_from_guid(guid)
            paths = [by_stage[key] for key in ("vertex", "fragment") if key in by_stage]
        if not paths:
            raise FileNotFoundError(f"ShaderInfo asset is not registered: {selector}")
        for path in paths:
            result = AssetManager.reimport_asset(path, database=database)
            if not result:
                raise RuntimeError(f"Shader reload failed for {path}: {result.error}")
        return True
