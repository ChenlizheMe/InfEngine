"""Shared command-backed menus for native editor surfaces."""

from __future__ import annotations

from collections.abc import Callable

from Infernux.engine.interaction import (
    ContextMenuCommand,
    ContextMenuSubmenu,
)


Translate = Callable[[str], str]


def _create_object(
    translate: Translate,
    label_key: str,
    kind: str,
    parent_id: int,
    semantic_id: str,
) -> ContextMenuCommand:
    return ContextMenuCommand(
        "scene.create_object",
        label=translate(label_key),
        payload={"kind": kind, "parent_id": int(parent_id)},
        semantic_id=semantic_id,
    )


def _hierarchy_create_entries(
    translate: Translate,
    parent_id: int,
    *,
    semantic_root: str,
):
    ui = _create_object(
        translate,
        "hierarchy.ui_canvas",
        "ui.canvas",
        parent_id,
        f"{semantic_root}.ui.canvas",
    )
    primitive_specs = (
        ("hierarchy.primitive_cube", "primitive.cube", "cube"),
        ("hierarchy.primitive_sphere", "primitive.sphere", "sphere"),
        ("hierarchy.primitive_capsule", "primitive.capsule", "capsule"),
        ("hierarchy.primitive_cylinder", "primitive.cylinder", "cylinder"),
        ("hierarchy.primitive_plane", "primitive.plane", "plane"),
        ("hierarchy.primitive_quad", "primitive.quad", "quad"),
    )
    light_specs = (
        ("hierarchy.light_directional", "light.directional", "directional"),
        ("hierarchy.light_point", "light.point", "point"),
        ("hierarchy.light_spot", "light.spot", "spot"),
    )
    return (
        _create_object(
            translate,
            "hierarchy.empty_object",
            "empty",
            parent_id,
            f"{semantic_root}.empty",
        ),
        _create_object(
            translate,
            "hierarchy.camera",
            "rendering.camera",
            parent_id,
            f"{semantic_root}.camera",
        ),
        ContextMenuSubmenu(
            translate("hierarchy.create_3d_object"),
            tuple(
                _create_object(
                    translate,
                    label_key,
                    kind,
                    parent_id,
                    f"{semantic_root}.create_3d.{suffix}",
                )
                for label_key, kind, suffix in primitive_specs
            ),
            semantic_id=f"{semantic_root}.create_3d",
        ),
        ContextMenuSubmenu(
            translate("hierarchy.create_2d_object"),
            (
                _create_object(
                    translate,
                    "hierarchy.sprite_renderer",
                    "rendering.sprite_renderer",
                    parent_id,
                    f"{semantic_root}.create_2d.sprite_renderer",
                ),
            ),
            semantic_id=f"{semantic_root}.create_2d",
        ),
        ContextMenuSubmenu(
            translate("hierarchy.light_menu"),
            tuple(
                _create_object(
                    translate,
                    label_key,
                    kind,
                    parent_id,
                    f"{semantic_root}.light.{suffix}",
                )
                for label_key, kind, suffix in light_specs
            ),
            semantic_id=f"{semantic_root}.light",
        ),
        ContextMenuSubmenu(
            translate("hierarchy.effect_menu"),
            (
                _create_object(
                    translate,
                    "hierarchy.particle_system",
                    "effect.particle_system",
                    parent_id,
                    f"{semantic_root}.effect.particle_system",
                ),
            ),
            semantic_id=f"{semantic_root}.effect",
        ),
        ContextMenuSubmenu(
            translate("hierarchy.post_processing_menu"),
            (
                _create_object(
                    translate,
                    "hierarchy.render_stack",
                    "rendering.render_stack",
                    parent_id,
                    f"{semantic_root}.post_processing.render_stack",
                ),
            ),
            semantic_id=f"{semantic_root}.post_processing",
        ),
        ContextMenuSubmenu(
            translate("hierarchy.ui_menu"),
            (ui,),
            semantic_id=f"{semantic_root}.ui",
        ),
    )


def hierarchy_context_menu(
    translate: Translate,
    *,
    target_id: int = 0,
    target_is_prefab: bool = False,
    create_parent_id: int = 0,
):
    """Return the complete Hierarchy menu for one frozen right-click target."""
    target_id = int(target_id or 0)
    create_parent_id = int(create_parent_id or 0)
    if target_id:
        target = {"target_id": str(target_id), "object_id": target_id}
        entries = [
            ContextMenuSubmenu(
                translate("hierarchy.create_child"),
                _hierarchy_create_entries(
                    translate,
                    target_id,
                    semantic_root="hierarchy.context.create_child",
                ),
                semantic_id="hierarchy.context.create_child",
            ),
            ContextMenuCommand(
                "scene.create_empty_parent",
                label=translate("hierarchy.create_empty_parent"),
                semantic_id="hierarchy.context.create_empty_parent",
            ),
            ContextMenuCommand(
                "edit.copy",
                label=translate("project.copy"),
                separator_before=True,
                semantic_id="hierarchy.context.copy",
            ),
            ContextMenuCommand(
                "edit.cut",
                label=translate("project.cut"),
                semantic_id="hierarchy.context.cut",
            ),
            ContextMenuCommand(
                "edit.paste",
                label=translate("project.paste"),
                semantic_id="hierarchy.context.paste",
            ),
            ContextMenuCommand(
                "edit.rename",
                label=translate("hierarchy.rename"),
                payload=target,
                separator_before=True,
                semantic_id="hierarchy.context.rename",
            ),
            ContextMenuCommand(
                "prefab.save_as",
                label=translate("hierarchy.save_as_prefab"),
                payload=target,
                separator_before=True,
                semantic_id="hierarchy.context.prefab.save_as",
            ),
        ]
        if target_is_prefab:
            entries.append(
                ContextMenuSubmenu(
                    translate("hierarchy.prefab_label"),
                    (
                        ContextMenuCommand(
                            "prefab.select_asset",
                            label=translate("hierarchy.select_prefab_asset"),
                            payload=target,
                        ),
                        ContextMenuCommand(
                            "prefab.open",
                            label=translate("hierarchy.open_prefab"),
                            payload=target,
                        ),
                        ContextMenuCommand(
                            "prefab.apply",
                            label=translate("hierarchy.apply_all_overrides"),
                            payload=target,
                        ),
                        ContextMenuCommand(
                            "prefab.revert",
                            label=translate("hierarchy.revert_all_overrides"),
                            payload=target,
                        ),
                        ContextMenuCommand(
                            "prefab.unpack",
                            label=translate("hierarchy.unpack_prefab"),
                            payload=target,
                            separator_before=True,
                        ),
                    ),
                    separator_before=True,
                    semantic_id="hierarchy.context.prefab",
                )
            )
        entries.append(
            ContextMenuCommand(
                "edit.delete",
                label=translate("hierarchy.delete"),
                payload=target,
                separator_before=True,
                semantic_id="hierarchy.context.delete",
            )
        )
        return tuple(entries)

    entries = list(
        _hierarchy_create_entries(
            translate,
            create_parent_id,
            semantic_root="hierarchy.context",
        )
    )
    entries.extend(
        (
            ContextMenuCommand(
                "scene.create_empty_parent",
                label=translate("hierarchy.create_empty_parent"),
                separator_before=True,
                hide_when_disabled=True,
                semantic_id="hierarchy.context.create_empty_parent",
            ),
            ContextMenuCommand(
                "edit.copy",
                label=translate("project.copy"),
                separator_before=True,
                hide_when_disabled=True,
                semantic_id="hierarchy.context.copy",
            ),
            ContextMenuCommand(
                "edit.cut",
                label=translate("project.cut"),
                hide_when_disabled=True,
                semantic_id="hierarchy.context.cut",
            ),
            ContextMenuCommand(
                "edit.paste",
                label=translate("project.paste"),
                hide_when_disabled=True,
                semantic_id="hierarchy.context.paste",
            ),
            ContextMenuCommand(
                "edit.delete",
                label=translate("hierarchy.delete_selected"),
                separator_before=True,
                hide_when_disabled=True,
                semantic_id="hierarchy.context.delete",
            ),
        )
    )
    return tuple(entries)


def _asset_create(
    translate: Translate,
    label_key: str,
    kind: str,
    base_name: str,
    extension: str,
    variant: str = "",
    *,
    separator_before: bool = False,
):
    return ContextMenuCommand(
        "asset.create",
        label=translate(label_key),
        payload={
            "kind": kind,
            "base_name": base_name,
            "extension": extension,
            "variant": variant,
        },
        separator_before=separator_before,
        semantic_id=f"project.context.create.{label_key.removeprefix('project.')}",
    )


def project_context_menu(
    translate: Translate,
    *,
    target_path: str = "",
    reveal_path: str = "",
    current_path: str = "",
):
    """Return the Project menu for paths frozen when its popup opened."""
    effects = tuple(
        _asset_create(translate, label, "render_effect", base, ".effect", feature)
        for label, base, feature in (
            ("project.effect_bloom", "NewBloom", "infernux.post.bloom"),
            ("project.effect_tonemapping", "NewToneMapping", "infernux.post.tonemapping"),
            ("project.effect_color_adjustments", "NewColorAdjustments", "infernux.post.color_adjustments"),
            ("project.effect_chromatic_aberration", "NewChromaticAberration", "infernux.post.chromatic_aberration"),
            ("project.effect_film_grain", "NewFilmGrain", "infernux.post.film_grain"),
            ("project.effect_motion_blur", "NewMotionBlur", "infernux.post.motion_blur"),
            ("project.effect_temporal_aa", "NewTemporalAA", "infernux.post.temporal_aa"),
            ("project.effect_sharpen", "NewSharpen", "infernux.post.sharpen"),
            ("project.effect_vignette", "NewVignette", "infernux.post.vignette"),
            ("project.effect_white_balance", "NewWhiteBalance", "infernux.post.white_balance"),
            ("project.effect_pixelation", "NewPixelation", "infernux.route.pixelation"),
        )
    )
    create_entries = (
        ContextMenuCommand(
            "project.create_folder",
            label=translate("project.create_folder"),
            semantic_id="project.context.create.folder",
        ),
        _asset_create(
            translate, "project.create_script", "script", "NewComponent", ".py",
            separator_before=True,
        ),
        _asset_create(
            translate, "project.create_vert_shader", "shader", "NewShader", ".vert", "vert",
            separator_before=True,
        ),
        _asset_create(
            translate, "project.create_frag_shader", "shader", "NewShader", ".frag", "frag",
        ),
        _asset_create(
            translate, "project.create_material", "material", "NewMaterial", ".mat",
            separator_before=True,
        ),
        _asset_create(
            translate, "project.create_physic_material", "physic_material", "NewPhysicMaterial", ".physicMaterial",
        ),
        _asset_create(
            translate, "project.create_scene", "scene", "NewScene", ".scene",
            separator_before=True,
        ),
        _asset_create(
            translate, "project.create_particlegraph", "particle_graph", "NewParticleGraph", ".particlegraph",
            separator_before=True,
        ),
        ContextMenuSubmenu(
            translate("project.create_render_effect"),
            effects,
            semantic_id="project.context.create.render_effect",
        ),
        _asset_create(
            translate,
            "project.create_render_effect_group",
            "render_effect_group",
            "NewRenderEffectGroup",
            ".effectgroup",
        ),
    )
    entries = [
        ContextMenuSubmenu(
            translate("project.create_menu"),
            create_entries,
            semantic_id="project.context.create",
        )
    ]
    reveal_target = str(reveal_path or current_path or "").strip()
    if reveal_target:
        entries.append(
            ContextMenuCommand(
                "project.reveal_in_explorer",
                label=translate("project.reveal_in_explorer"),
                payload={"path": reveal_target},
                separator_before=True,
                semantic_id="project.context.reveal",
            )
        )
    if target_path:
        entries.extend(
            (
                ContextMenuCommand(
                    "edit.copy",
                    label=translate("project.copy"),
                    payload={"target_id": target_path},
                    separator_before=True,
                    semantic_id="project.context.copy",
                ),
                ContextMenuCommand(
                    "edit.cut",
                    label=translate("project.cut"),
                    payload={"target_id": target_path},
                    semantic_id="project.context.cut",
                ),
                ContextMenuCommand(
                    "edit.paste",
                    label=translate("project.paste"),
                    semantic_id="project.context.paste",
                ),
                ContextMenuCommand(
                    "edit.rename",
                    label=translate("project.rename"),
                    payload={"target_id": target_path},
                    separator_before=True,
                    semantic_id="project.context.rename",
                ),
                ContextMenuCommand(
                    "edit.delete",
                    label=translate("project.delete"),
                    payload={"target_id": target_path},
                    semantic_id="project.context.delete",
                ),
            )
        )
    else:
        entries.append(
            ContextMenuCommand(
                "edit.paste",
                label=translate("project.paste"),
                separator_before=True,
                semantic_id="project.context.paste",
            )
        )
    return tuple(entries)
