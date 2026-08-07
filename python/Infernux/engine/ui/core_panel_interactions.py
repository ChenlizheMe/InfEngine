"""Interaction descriptors for the editor's core panels."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Optional

from Infernux.engine.path_utils import lexical_path
from Infernux.engine.interaction import (
    ActionOrigin,
    BoundPanelCommand,
    CommandSource,
    CommandContext,
    ExternalDropKind,
    KeyChord,
    PanelCommandAdapter,
    PanelCommandSpec,
    PanelInteractionDescriptor,
    PanelShortcutSpec,
    SelectionDomain,
    SelectionService,
    SelectionTarget,
    NavigationService,
    SceneObjectCommandService,
    ProjectAssetInteractionService,
    ViewCommandService,
    TreeViewStateService,
)


def _require_methods(instance: object, names: tuple[str, ...], label: str) -> None:
    missing = tuple(
        name for name in names if not callable(getattr(instance, name, None))
    )
    if missing:
        raise TypeError(f"{label} interaction contract is missing: {missing}")


def _deselect(context: CommandContext, reason: str) -> bool:
    return SelectionService.instance().clear(
        reason=reason,
        record_history=True,
    )


def _can_deselect(context: CommandContext) -> bool:
    return bool(context.selection.targets)


def _begin_hierarchy_rename(panel: object, context: CommandContext) -> bool:
    """Open the hierarchy's inline rename presentation for one object."""
    target_id = str(context.payload.get("target_id", "") or "").strip()
    if not target_id and context.selection.primary is not None:
        target_id = context.selection.primary.target_id
    try:
        object_id = int(target_id)
    except (TypeError, ValueError):
        return False
    if object_id <= 0:
        return False
    panel.begin_rename_object(object_id)
    return True


def _standard_edit_shortcuts(*, rename: bool = False) -> tuple[PanelShortcutSpec, ...]:
    pairs = [
        ("edit.copy", "Ctrl+C"),
        ("edit.cut", "Ctrl+X"),
        ("edit.paste", "Ctrl+V"),
        ("edit.delete", "Delete"),
    ]
    if rename:
        pairs.append(("edit.rename", "F2"))
    pairs.append(("edit.deselect", "Escape"))
    return tuple(
        PanelShortcutSpec(command_id, KeyChord.parse(chord))
        for command_id, chord in pairs
    )


def hierarchy_panel_interaction(
    scene_commands: SceneObjectCommandService,
    *,
    creation_service: Optional[object] = None,
    tree_views: Optional[TreeViewStateService] = None,
) -> PanelInteractionDescriptor:
    tree_state = tree_views or TreeViewStateService.require()

    def creation_args(context: CommandContext):
        kind = str(context.payload.get("kind", "") or "").strip()
        try:
            parent_id = int(context.payload.get("parent_id", 0) or 0)
        except (AttributeError, TypeError, ValueError):
            return None
        return (kind, parent_id) if kind and parent_id >= 0 else None

    def external_drop_args(context: CommandContext):
        payload = context.payload
        reference = str(payload.get("reference", "") or "").strip()
        try:
            parent_id = int(payload.get("parent_id", 0) or 0)
        except (TypeError, ValueError):
            return None
        return (
            reference,
            parent_id,
            bool(payload.get("is_guid", False)),
        ) if reference and parent_id >= 0 else None

    def rename_args(context: CommandContext):
        payload = context.payload
        try:
            object_id = int(payload.get("object_id", 0) or 0)
        except (TypeError, ValueError):
            return None
        new_name = str(payload.get("new_name", "") or "").strip()
        return (object_id, new_name) if object_id > 0 and new_name else None

    def move_args(context: CommandContext):
        payload = context.payload
        try:
            object_ids = tuple(int(value) for value in payload.get("object_ids", ()))
            target_id = int(payload.get("target_id", 0) or 0)
        except (TypeError, ValueError):
            return None
        mode = str(payload.get("mode", "") or "").strip().lower()
        if not object_ids or any(value <= 0 for value in object_ids):
            return None
        if mode not in {"parent", "adjacent", "root"}:
            return None
        if mode != "root" and target_id <= 0:
            return None
        return object_ids, mode, target_id, bool(payload.get("after", False))

    def expanded_args(context: CommandContext):
        try:
            object_id = int(context.payload.get("target_id", 0) or 0)
        except (TypeError, ValueError):
            return None
        expanded = context.payload.get("expanded")
        if object_id <= 0 or not isinstance(expanded, bool):
            return None
        return object_id, expanded

    def bind(panel: object) -> PanelCommandAdapter:
        _require_methods(
            panel,
            (
                "begin_rename_object",
                "get_expanded_object_ids",
                "set_expanded_object_ids",
            ),
            "hierarchy panel",
        )
        handlers = {
                "edit.copy": BoundPanelCommand(
                    lambda context: scene_commands.copy(context, cut=False),
                    scene_commands.can_copy,
                ),
                "edit.cut": BoundPanelCommand(
                    lambda context: scene_commands.copy(context, cut=True),
                    scene_commands.can_copy,
                ),
                "edit.paste": BoundPanelCommand(
                    scene_commands.paste,
                    scene_commands.can_paste,
                ),
                "edit.delete": BoundPanelCommand(
                    scene_commands.delete,
                    scene_commands.has_selection,
                ),
                "edit.rename": BoundPanelCommand(
                    lambda context: _begin_hierarchy_rename(panel, context),
                    scene_commands.has_selection,
                ),
                "edit.deselect": BoundPanelCommand(
                    lambda context: _deselect(context, "hierarchy_deselect"),
                    _can_deselect,
                ),
                "scene.instantiate_prefab": BoundPanelCommand(
                    lambda context: bool(
                        (args := external_drop_args(context)) is not None
                        and scene_commands.instantiate_prefab(*args)
                    ),
                    lambda context: bool(
                        (args := external_drop_args(context)) is not None
                        and scene_commands.can_external_drop(*args)
                    ),
                ),
                "scene.create_model": BoundPanelCommand(
                    lambda context: bool(
                        (args := external_drop_args(context)) is not None
                        and scene_commands.create_model(*args)
                    ),
                    lambda context: bool(
                        (args := external_drop_args(context)) is not None
                        and scene_commands.can_external_drop(*args)
                    ),
                ),
                "scene.rename_object": BoundPanelCommand(
                    lambda context: bool(
                        (args := rename_args(context)) is not None
                        and scene_commands.rename(*args)
                    ),
                    lambda context: rename_args(context) is not None,
                ),
                "scene.move_hierarchy": BoundPanelCommand(
                    lambda context: bool(
                        (args := move_args(context)) is not None
                        and scene_commands.move_hierarchy(*args)
                    ),
                    lambda context: move_args(context) is not None,
                ),
                "hierarchy.set_expanded": BoundPanelCommand(
                    lambda context: bool(
                        (args := expanded_args(context)) is not None
                        and tree_state.set_expanded(
                            panel.get_expanded_object_ids(),
                            args[0],
                            args[1],
                            panel.set_expanded_object_ids,
                            description=(
                                "Expand Hierarchy Item"
                                if args[1]
                                else "Collapse Hierarchy Item"
                            ),
                        )
                    ),
                    lambda context: bool(
                        (args := expanded_args(context)) is not None
                        and (args[0] in panel.get_expanded_object_ids()) != args[1]
                    ),
                ),
        }
        if creation_service is not None:
            def can_create(context: CommandContext) -> bool:
                args = creation_args(context)
                return bool(
                    args is not None
                    and creation_service.can_create(args[0], parent_id=args[1])
                )

            def create(context: CommandContext) -> bool:
                args = creation_args(context)
                if args is None:
                    return False
                creation_service.create(args[0], parent_id=args[1])
                return True

            def selected_object_ids(context: CommandContext) -> list[int]:
                return [
                    target.scene_object_id()
                    for target in context.selection.targets
                    if target.domain is SelectionDomain.SCENE_OBJECT
                    and target.scene_object_id() > 0
                ]

            handlers["scene.create_object"] = BoundPanelCommand(
                create,
                can_create,
            )
            handlers["scene.create_empty_parent"] = BoundPanelCommand(
                lambda context: bool(
                    creation_service.create_empty_parent(selected_object_ids(context))
                ),
                lambda context: creation_service.can_create_empty_parent(
                    selected_object_ids(context)
                ),
            )
        return PanelCommandAdapter(handlers)

    command_ids = [
        "edit.copy",
        "edit.cut",
        "edit.paste",
        "edit.delete",
        "edit.rename",
        "edit.deselect",
        "scene.instantiate_prefab",
        "scene.create_model",
        "scene.rename_object",
        "scene.move_hierarchy",
        "hierarchy.set_expanded",
    ]
    if creation_service is not None:
        command_ids.extend(("scene.create_object", "scene.create_empty_parent"))

    return PanelInteractionDescriptor(
        commands=tuple(
            PanelCommandSpec(command_id)
            for command_id in command_ids
        ),
        shortcuts=_standard_edit_shortcuts(rename=True),
        owned_selection_domains=frozenset({SelectionDomain.SCENE_OBJECT}),
        adapter_factory=bind,
    )


def project_panel_interaction(
    interactions: ProjectAssetInteractionService,
    navigation: NavigationService,
    tree_views: Optional[TreeViewStateService] = None,
) -> PanelInteractionDescriptor:
    tree_state = tree_views or TreeViewStateService.require()

    def origin(context: CommandContext) -> ActionOrigin:
        return (
            ActionOrigin.AUTOMATION
            if context.source is CommandSource.AUTOMATION
            else ActionOrigin.USER
        )

    def selected_paths(context: CommandContext) -> tuple[str, ...]:
        result: list[str] = []
        seen: set[str] = set()
        if context.selection.domain is SelectionDomain.ASSET:
            raw_paths = tuple(
                str(target.document_id or target.target_id or "").strip()
                for target in context.selection.targets
            )
        else:
            raw_paths = ()
        for path in raw_paths:
            path = lexical_path(path)
            key = path.casefold()
            if not path or key in seen:
                continue
            seen.add(key)
            result.append(path)
        explicit = context.payload.get("paths", ())
        if isinstance(explicit, str):
            explicit = (explicit,)
        explicit_paths = tuple(
            lexical_path(str(path or "").strip())
            for path in explicit
            if str(path or "").strip()
        )
        target = lexical_path(
            str(context.payload.get("target_id", "") or "").strip()
        )
        if target and target.casefold() not in seen:
            explicit_paths = (target,)
        if explicit_paths and not all(
            path.casefold() in seen for path in explicit_paths
        ):
            return tuple(dict.fromkeys(explicit_paths))
        return tuple(result)

    def target_path(context: CommandContext) -> str:
        value = str(context.payload.get("target_id", "") or "").strip()
        if (
            not value
            and context.selection.domain is SelectionDomain.ASSET
            and context.selection.primary is not None
        ):
            value = context.selection.primary.target_id
        return value

    def destination(context: CommandContext, panel: object) -> str:
        return str(
            context.payload.get("directory", "")
            or context.payload.get("destination", "")
            or panel.get_current_path()
            or ""
        ).strip()

    def create_asset(context: CommandContext, panel: object) -> bool:
        payload = context.payload
        path = interactions.create(
            str(payload.get("kind", "") or ""),
            destination(context, panel),
            str(payload.get("base_name", "") or ""),
            str(payload.get("extension", "") or ""),
            str(payload.get("variant", "") or ""),
            origin=origin(context),
        )
        return bool(path and panel.begin_rename_selected_asset(path))

    def create_folder(context: CommandContext, panel: object) -> bool:
        path = interactions.create(
            "folder",
            destination(context, panel),
            "NewFolder",
            "",
            origin=origin(context),
        )
        return bool(path and panel.begin_rename_selected_asset(path))

    def can_create_asset(context: CommandContext, panel: object) -> bool:
        return bool(
            interactions.can_create(
                str(context.payload.get("kind", "") or ""),
                destination(context, panel),
                str(context.payload.get("base_name", "") or ""),
                str(context.payload.get("extension", "") or ""),
            )
        )

    def open_asset(context: CommandContext, panel: object) -> bool:
        return interactions.open(
            str(context.payload.get("kind", "") or ""),
            str(context.payload.get("path", "") or ""),
        )

    def reveal_in_explorer(context: CommandContext, panel: object) -> bool:
        path = str(context.payload.get("path", "") or "").strip()
        return bool(path and interactions.reveal(path))

    def can_open_asset(context: CommandContext) -> bool:
        return bool(
            interactions.can_open(
                str(context.payload.get("kind", "") or ""),
                str(context.payload.get("path", "") or ""),
            )
        )

    def transfer(context: CommandContext, panel: object) -> bool:
        raw_paths = context.payload.get("paths", ())
        if isinstance(raw_paths, str):
            raw_paths = (raw_paths,)
        paths = tuple(
            str(path or "").strip()
            for path in raw_paths
            if str(path or "").strip()
        )
        target = destination(context, panel)
        return bool(paths and interactions.transfer(paths, target, origin=origin(context)))

    def navigation_path(context: CommandContext) -> str:
        return lexical_path(
            str(
                context.payload.get("path", "")
                or context.payload.get("target_id", "")
                or ""
            ).strip()
        )

    def expansion_args(context: CommandContext):
        path = navigation_path(context)
        expanded = context.payload.get("expanded")
        if not path or not isinstance(expanded, bool):
            return None
        return path, expanded

    def set_tree_expanded(
        context: CommandContext,
        panel: object,
        *,
        getter: str,
        setter: str,
        label: str,
    ) -> bool:
        args = expansion_args(context)
        if args is None:
            return False
        return tree_state.set_expanded(
            getattr(panel, getter)(),
            args[0],
            args[1],
            getattr(panel, setter),
            description=f"{'Expand' if args[1] else 'Collapse'} {label}",
        )

    def can_set_tree_expanded(
        context: CommandContext,
        panel: object,
        *,
        getter: str,
    ) -> bool:
        args = expansion_args(context)
        return bool(
            args is not None
            and (args[0] in getattr(panel, getter)()) != args[1]
        )

    def can_navigate_directory(context: CommandContext, panel: object) -> bool:
        path = navigation_path(context)
        return bool(
            path
            and panel.can_navigate_to_path(path)
            and lexical_path(panel.get_current_path()).casefold() != path.casefold()
        )

    def navigate_directory(context: CommandContext, panel: object) -> bool:
        path = navigation_path(context)
        return ViewCommandService.require().set_value(
            lexical_path(panel.get_current_path()),
            path,
            panel.set_current_path,
            description="Navigate Project",
        )

    def can_locate_asset(context: CommandContext) -> bool:
        path = navigation_path(context)
        return bool(path and interactions.can_copy((path,)))

    def locate_asset(context: CommandContext) -> bool:
        path = navigation_path(context)
        return navigation.locate(
            SelectionTarget.asset(path),
            owner_id="project",
            reason="project_locate_asset",
            record_history=True,
        )

    def bind(panel: object) -> PanelCommandAdapter:
        _require_methods(
            panel,
            (
                "begin_rename_selected_asset",
                "can_rename_selected_asset",
                "can_navigate_to_path",
                "get_current_path",
                "set_current_path",
                "get_folder_expanded_paths",
                "set_folder_expanded_paths",
                "get_model_expanded_paths",
                "set_model_expanded_paths",
            ),
            "project panel",
        )
        return PanelCommandAdapter(
            {
                "edit.copy": BoundPanelCommand(
                    lambda context: interactions.copy(selected_paths(context), cut=False),
                    lambda context: interactions.can_copy(selected_paths(context)),
                ),
                "edit.cut": BoundPanelCommand(
                    lambda context: interactions.copy(selected_paths(context), cut=True),
                    lambda context: interactions.can_copy(selected_paths(context)),
                ),
                "edit.paste": BoundPanelCommand(
                    lambda context: bool(
                        interactions.paste(
                            destination(context, panel),
                            origin=origin(context),
                        )
                    ),
                    lambda context: interactions.can_paste(
                        destination(context, panel)
                    ),
                ),
                "edit.delete": BoundPanelCommand(
                    lambda context: interactions.request_delete(
                        selected_paths(context),
                        origin=origin(context),
                    ),
                    lambda context: interactions.can_copy(selected_paths(context)),
                ),
                "edit.rename": BoundPanelCommand(
                    lambda context: panel.begin_rename_selected_asset(
                        target_path(context)
                    ),
                    lambda context: bool(
                        interactions.can_copy(selected_paths(context))
                        and panel.can_rename_selected_asset(target_path(context))
                    ),
                ),
                "edit.deselect": BoundPanelCommand(
                    lambda context: _deselect(context, "project_deselect"),
                    _can_deselect,
                ),
                "project.create_folder": BoundPanelCommand(
                    lambda context: create_folder(context, panel),
                    lambda context: interactions.can_create(
                        "folder",
                        destination(context, panel),
                        "NewFolder",
                        "",
                    ),
                ),
                "asset.create": BoundPanelCommand(
                    lambda context: create_asset(context, panel),
                    lambda context: can_create_asset(context, panel),
                ),
                "asset.open": BoundPanelCommand(
                    lambda context: open_asset(context, panel),
                    can_open_asset,
                ),
                "project.reveal_in_explorer": BoundPanelCommand(
                    lambda context: reveal_in_explorer(context, panel),
                    lambda context: bool(
                        str(context.payload.get("path", "") or "").strip()
                    ),
                ),
                "asset.transfer": BoundPanelCommand(
                    lambda context: transfer(context, panel),
                    lambda context: bool(
                        context.payload.get("paths")
                        and destination(context, panel)
                    ),
                ),
                "project.navigate_directory": BoundPanelCommand(
                    lambda context: navigate_directory(context, panel),
                    lambda context: can_navigate_directory(context, panel),
                ),
                "project.locate_asset": BoundPanelCommand(
                    locate_asset,
                    can_locate_asset,
                ),
                "project.set_folder_expanded": BoundPanelCommand(
                    lambda context: set_tree_expanded(
                        context,
                        panel,
                        getter="get_folder_expanded_paths",
                        setter="set_folder_expanded_paths",
                        label="Project Folder",
                    ),
                    lambda context: can_set_tree_expanded(
                        context,
                        panel,
                        getter="get_folder_expanded_paths",
                    ),
                ),
                "project.set_model_expanded": BoundPanelCommand(
                    lambda context: set_tree_expanded(
                        context,
                        panel,
                        getter="get_model_expanded_paths",
                        setter="set_model_expanded_paths",
                        label="Model Contents",
                    ),
                    lambda context: can_set_tree_expanded(
                        context,
                        panel,
                        getter="get_model_expanded_paths",
                    ),
                ),
            }
        )

    commands = (
        "edit.copy",
        "edit.cut",
        "edit.paste",
        "edit.delete",
        "edit.rename",
        "edit.deselect",
        "project.create_folder",
        "asset.create",
        "asset.open",
        "project.reveal_in_explorer",
        "asset.transfer",
        "project.navigate_directory",
        "project.locate_asset",
        "project.set_folder_expanded",
        "project.set_model_expanded",
    )
    return PanelInteractionDescriptor(
        commands=tuple(PanelCommandSpec(command_id) for command_id in commands),
        shortcuts=_standard_edit_shortcuts(rename=True)
        + (
            PanelShortcutSpec(
                "project.create_folder",
                KeyChord.parse("Ctrl+Shift+N"),
            ),
        ),
        owned_selection_domains=frozenset(
            {SelectionDomain.ASSET, SelectionDomain.ASSET_SUBRESOURCE}
        ),
        external_drop_kinds=frozenset({ExternalDropKind.FILES}),
        adapter_factory=bind,
    )


def inspector_panel_interaction(
    actions_getter: Callable[[], object],
) -> PanelInteractionDescriptor:
    def actions():
        return actions_getter()

    def component_selected(context: CommandContext) -> bool:
        target = component_target(context)
        return bool(
            (
                target is not None
                or (
                    context.selection.domain is SelectionDomain.COMPONENT
                    and context.selection.targets
                )
            )
            and actions() is not None
        )

    def component_target(context: CommandContext):
        payload = context.payload
        if "object_id" not in payload and "component_id" not in payload:
            return None
        try:
            object_id = int(payload.get("object_id", 0) or 0)
            component_id = int(payload.get("component_id", 0) or 0)
        except (TypeError, ValueError):
            return None
        if object_id <= 0 or component_id <= 0:
            return None
        return object_id, component_id, bool(payload.get("is_native", False))

    def reorder_args(context: CommandContext):
        payload = context.payload
        try:
            object_ids = tuple(int(value) for value in payload.get("object_ids", ()))
            dragged_ids = tuple(
                int(value) for value in payload.get("dragged_component_ids", ())
            )
            target_ids = tuple(
                int(value) for value in payload.get("target_component_ids", ())
            )
        except (TypeError, ValueError):
            return None
        if (
            not object_ids
            or len(object_ids) != len(dragged_ids)
            or len(object_ids) != len(target_ids)
            or any(value <= 0 for value in object_ids + dragged_ids + target_ids)
        ):
            return None
        return object_ids, dragged_ids, target_ids, bool(
            payload.get("insert_after", False)
        )

    def add_args(context: CommandContext):
        payload = context.payload
        type_name = str(payload.get("type_name", "") or "").strip()
        script_path = str(payload.get("script_path", "") or "").strip()
        if not type_name:
            return None
        try:
            target_component_id = int(payload.get("target_component_id", 0) or 0)
        except (TypeError, ValueError):
            return None
        insert_at_start = bool(payload.get("insert_at_start", False))
        if target_component_id < 0 or (target_component_id and insert_at_start):
            return None
        return (
            type_name,
            bool(payload.get("is_native", False)),
            script_path,
            target_component_id,
            bool(payload.get("insert_after", False)),
            insert_at_start,
        )

    def enabled_args(context: CommandContext):
        payload = context.payload
        raw_targets = payload.get("targets", ())
        if not isinstance(raw_targets, (list, tuple)) or not raw_targets:
            return None
        try:
            object_ids = tuple(int(item.get("object_id", 0) or 0) for item in raw_targets)
            component_ids = tuple(
                int(item.get("component_id", 0) or 0) for item in raw_targets
            )
        except (AttributeError, TypeError, ValueError):
            return None
        if (
            len(object_ids) != len(component_ids)
            or any(value <= 0 for value in object_ids + component_ids)
        ):
            return None
        return (
            object_ids,
            component_ids,
            bool(payload.get("enabled", False)),
            bool(payload.get("is_native", False)),
        )

    def bind(_panel: object) -> PanelCommandAdapter:
        def can_reorder(context: CommandContext) -> bool:
            value = actions()
            args = reorder_args(context)
            return bool(
                component_selected(context)
                and args is not None
                and value.can_reorder(*args)
            )

        def reorder(context: CommandContext) -> bool:
            value = actions()
            args = reorder_args(context)
            return bool(value is not None and args is not None and value.reorder(*args))

        def can_add(context: CommandContext) -> bool:
            value = actions()
            args = add_args(context)
            return bool(value is not None and args is not None and value.can_add(*args))

        def add(context: CommandContext) -> bool:
            value = actions()
            args = add_args(context)
            return bool(value is not None and args is not None and value.add(*args))

        def can_set_enabled(context: CommandContext) -> bool:
            value = actions()
            args = enabled_args(context)
            return bool(
                value is not None
                and args is not None
                and value.can_set_enabled(*args)
            )

        def set_enabled(context: CommandContext) -> bool:
            value = actions()
            args = enabled_args(context)
            return bool(
                value is not None
                and args is not None
                and value.set_enabled(*args)
            )

        return PanelCommandAdapter(
            {
                "edit.copy": BoundPanelCommand(
                    lambda context: actions().copy(component_target(context)),
                    lambda context: bool(
                        component_selected(context)
                        and actions().can_copy(component_target(context))
                    ),
                ),
                "edit.paste": BoundPanelCommand(
                    lambda context: actions().paste_default(component_target(context)),
                    lambda context: bool(
                        component_selected(context)
                        and (
                            actions().can_paste_values(component_target(context))
                            or actions().can_paste_as_new(component_target(context))
                        )
                    ),
                ),
                "edit.delete": BoundPanelCommand(
                    lambda context: actions().remove(component_target(context)),
                    lambda context: bool(
                        component_selected(context)
                        and actions().can_remove(component_target(context))
                    ),
                ),
                "edit.deselect": BoundPanelCommand(
                    lambda context: _deselect(context, "inspector_deselect"),
                    _can_deselect,
                ),
                "component.open_script": BoundPanelCommand(
                    lambda context: actions().open_script(component_target(context)),
                    lambda context: bool(
                        component_selected(context)
                        and actions().can_open_script(component_target(context))
                    ),
                ),
                "component.copy_properties": BoundPanelCommand(
                    lambda context: actions().copy(component_target(context)),
                    lambda context: bool(
                        component_selected(context)
                        and actions().can_copy(component_target(context))
                    ),
                ),
                "component.paste_properties": BoundPanelCommand(
                    lambda context: actions().paste_values(component_target(context)),
                    lambda context: bool(
                        component_selected(context)
                        and actions().can_paste_values(component_target(context))
                    ),
                ),
                "component.paste_as_new": BoundPanelCommand(
                    lambda context: actions().paste_as_new(component_target(context)),
                    lambda context: bool(
                        component_selected(context)
                        and actions().can_paste_as_new(component_target(context))
                    ),
                ),
                "component.remove": BoundPanelCommand(
                    lambda context: actions().remove(component_target(context)),
                    lambda context: bool(
                        component_selected(context)
                        and actions().can_remove(component_target(context))
                    ),
                ),
                "component.reset": BoundPanelCommand(
                    lambda context: actions().reset(component_target(context)),
                    lambda context: bool(
                        component_selected(context)
                        and actions().can_reset(component_target(context))
                    ),
                ),
                "component.move_up": BoundPanelCommand(
                    lambda context: actions().move_up(component_target(context)),
                    lambda context: bool(
                        component_selected(context)
                        and actions().can_move_up(component_target(context))
                    ),
                ),
                "component.move_down": BoundPanelCommand(
                    lambda context: actions().move_down(component_target(context)),
                    lambda context: bool(
                        component_selected(context)
                        and actions().can_move_down(component_target(context))
                    ),
                ),
                "component.reorder": BoundPanelCommand(reorder, can_reorder),
                "component.add": BoundPanelCommand(add, can_add),
                "component.set_enabled": BoundPanelCommand(
                    set_enabled,
                    can_set_enabled,
                ),
            }
        )

    commands = (
        "edit.copy",
        "edit.paste",
        "edit.delete",
        "edit.deselect",
        "component.open_script",
        "component.copy_properties",
        "component.paste_properties",
        "component.paste_as_new",
        "component.remove",
        "component.reset",
        "component.move_up",
        "component.move_down",
        "component.reorder",
        "component.add",
        "component.set_enabled",
    )
    return PanelInteractionDescriptor(
        commands=tuple(PanelCommandSpec(command_id) for command_id in commands),
        shortcuts=tuple(
            PanelShortcutSpec(command_id, KeyChord.parse(chord))
            for command_id, chord in (
                ("edit.copy", "Ctrl+C"),
                ("edit.paste", "Ctrl+V"),
                ("edit.delete", "Delete"),
                ("edit.deselect", "Escape"),
            )
        ),
        owned_selection_domains=frozenset(
            {
                SelectionDomain.SCENE_OBJECT,
                SelectionDomain.COMPONENT,
                SelectionDomain.ASSET,
                SelectionDomain.ASSET_SUBRESOURCE,
            }
        ),
        adapter_factory=bind,
    )


def scene_view_panel_interaction(
    scene_commands: SceneObjectCommandService,
) -> PanelInteractionDescriptor:
    from Infernux.engine.interaction import ViewCommandService

    tool_specs = {
        "scene.tool.select": (0, "Select Scene Tool"),
        "scene.tool.move": (1, "Select Move Tool"),
        "scene.tool.rotate": (2, "Select Rotate Tool"),
        "scene.tool.scale": (3, "Select Scale Tool"),
    }

    def bind(panel: object) -> PanelCommandAdapter:
        _require_methods(
            panel,
            (
                "_set_tool_mode",
                "_set_coordinate_space",
                "_align_object_to_camera",
                "can_frame_object_by_id",
                "frame_object_by_id",
            ),
            "scene view panel",
        )

        def frame_target_id(context: CommandContext) -> int:
            value = context.payload.get(
                "object_id",
                context.payload.get("target_id", 0),
            )
            try:
                object_id = int(value or 0)
            except (TypeError, ValueError):
                object_id = 0
            if object_id <= 0 and context.selection.primary is not None:
                primary = context.selection.primary
                if primary.domain is SelectionDomain.SCENE_OBJECT:
                    object_id = primary.scene_object_id()
            return object_id

        def set_tool(mode: int, description: str) -> bool:
            old_mode = int(panel._gizmo_tool_mode)
            if old_mode == int(mode):
                return False
            return ViewCommandService.require().set_value(
                old_mode,
                int(mode),
                panel._set_tool_mode,
                description=description,
            )

        def coordinate_space(context: CommandContext) -> int:
            try:
                return int(context.payload.get("value", -1))
            except (TypeError, ValueError):
                return -1

        def set_coordinate_space(context: CommandContext) -> bool:
            value = coordinate_space(context)
            old_value = int(panel._coord_space)
            if value not in (0, 1) or value == old_value:
                return False
            return ViewCommandService.require().set_value(
                old_value,
                value,
                panel._set_coordinate_space,
                description="Set Scene Coordinate Space",
            )

        handlers = {
            "edit.copy": BoundPanelCommand(
                lambda context: scene_commands.copy(context, cut=False),
                scene_commands.can_copy,
            ),
            "edit.cut": BoundPanelCommand(
                lambda context: scene_commands.copy(context, cut=True),
                scene_commands.can_copy,
            ),
            "edit.paste": BoundPanelCommand(
                scene_commands.paste,
                scene_commands.can_paste,
            ),
            "edit.delete": BoundPanelCommand(
                scene_commands.delete,
                scene_commands.has_selection,
            ),
            "edit.deselect": BoundPanelCommand(
                lambda context: _deselect(context, "scene_view_deselect"),
                _can_deselect,
            ),
            "scene.align_to_camera": BoundPanelCommand(
                lambda _context: panel._align_object_to_camera(),
                scene_commands.has_selection,
            ),
            "scene.frame_selected": BoundPanelCommand(
                lambda context: panel.frame_object_by_id(frame_target_id(context)),
                lambda context: panel.can_frame_object_by_id(frame_target_id(context)),
            ),
            "scene.set_coordinate_space": BoundPanelCommand(
                set_coordinate_space,
                lambda context: (
                    coordinate_space(context) in (0, 1)
                    and coordinate_space(context) != int(panel._coord_space)
                ),
            ),
        }
        handlers.update(
            {
                command_id: BoundPanelCommand(
                    lambda _context, _mode=mode, _description=description: set_tool(
                        _mode, _description
                    ),
                    lambda _context: True,
                )
                for command_id, (mode, description) in tool_specs.items()
            }
        )
        return PanelCommandAdapter(handlers)

    commands = (
        "edit.copy",
        "edit.cut",
        "edit.paste",
        "edit.delete",
        "edit.deselect",
        *tool_specs,
        "scene.align_to_camera",
        "scene.frame_selected",
        "scene.set_coordinate_space",
    )
    return PanelInteractionDescriptor(
        commands=tuple(PanelCommandSpec(command_id) for command_id in commands),
        shortcuts=_standard_edit_shortcuts()
        + tuple(
            PanelShortcutSpec(command_id, KeyChord.parse(chord))
            for command_id, chord in (
                ("scene.tool.select", "Q"),
                ("scene.tool.move", "W"),
                ("scene.tool.rotate", "E"),
                ("scene.tool.scale", "R"),
                ("scene.align_to_camera", "Ctrl+F"),
                ("scene.frame_selected", "F"),
            )
        ),
        owned_selection_domains=frozenset({SelectionDomain.SCENE_OBJECT}),
        adapter_factory=bind,
    )


def ui_editor_panel_interaction(
    scene_commands: SceneObjectCommandService,
    creation_service: Optional[object] = None,
) -> PanelInteractionDescriptor:
    nudge_specs = {
        "ui.nudge.left": (-1, 0),
        "ui.nudge.right": (1, 0),
        "ui.nudge.up": (0, -1),
        "ui.nudge.down": (0, 1),
        "ui.nudge.left.fast": (-10, 0),
        "ui.nudge.right.fast": (10, 0),
        "ui.nudge.up.fast": (0, -10),
        "ui.nudge.down.fast": (0, 10),
    }

    def creation_args(context: CommandContext):
        kind = str(context.payload.get("kind", "") or "").strip()
        try:
            parent_id = int(context.payload.get("parent_id", 0) or 0)
        except (TypeError, ValueError):
            return None
        if kind not in {"ui.canvas", "ui.text", "ui.image", "ui.button"}:
            return None
        return kind, parent_id

    def bind(panel: object) -> PanelCommandAdapter:
        _require_methods(
            panel,
            ("can_nudge_selected", "command_nudge_selected"),
            "UI editor panel",
        )
        handlers = {
            "edit.copy": BoundPanelCommand(
                lambda context: scene_commands.copy(context, cut=False),
                scene_commands.can_copy,
            ),
            "edit.cut": BoundPanelCommand(
                lambda context: scene_commands.copy(context, cut=True),
                scene_commands.can_copy,
            ),
            "edit.paste": BoundPanelCommand(
                scene_commands.paste,
                scene_commands.can_paste,
            ),
            "edit.delete": BoundPanelCommand(
                scene_commands.delete,
                scene_commands.has_selection,
            ),
            "edit.deselect": BoundPanelCommand(
                lambda context: _deselect(context, "ui_editor_deselect"),
                _can_deselect,
            ),
        }
        if creation_service is not None:
            handlers["scene.create_object"] = BoundPanelCommand(
                lambda context: bool(
                    (args := creation_args(context)) is not None
                    and creation_service.create(
                        args[0],
                        parent_id=args[1],
                        selection_owner_id="ui_editor",
                        selection_reason=f"ui_editor_create_{args[0].rsplit('.', 1)[-1]}",
                    )
                ),
                lambda context: bool(
                    (args := creation_args(context)) is not None
                    and creation_service.can_create(args[0], parent_id=args[1])
                ),
            )
        handlers.update(
            {
                command_id: BoundPanelCommand(
                    lambda _context, _dx=dx, _dy=dy: panel.command_nudge_selected(
                        _dx, _dy
                    ),
                    lambda _context: panel.can_nudge_selected(),
                )
                for command_id, (dx, dy) in nudge_specs.items()
            }
        )
        return PanelCommandAdapter(handlers)

    commands = (
        "edit.copy",
        "edit.cut",
        "edit.paste",
        "edit.delete",
        "edit.deselect",
        *nudge_specs,
        *(("scene.create_object",) if creation_service is not None else ()),
    )
    return PanelInteractionDescriptor(
        commands=tuple(PanelCommandSpec(command_id) for command_id in commands),
        shortcuts=_standard_edit_shortcuts()
        + tuple(
            PanelShortcutSpec(command_id, KeyChord.parse(chord))
            for command_id, chord in (
                ("ui.nudge.left", "Left"),
                ("ui.nudge.right", "Right"),
                ("ui.nudge.up", "Up"),
                ("ui.nudge.down", "Down"),
                ("ui.nudge.left.fast", "Shift+Left"),
                ("ui.nudge.right.fast", "Shift+Right"),
                ("ui.nudge.up.fast", "Shift+Up"),
                ("ui.nudge.down.fast", "Shift+Down"),
            )
        ),
        owned_selection_domains=frozenset(
            {SelectionDomain.SCENE_OBJECT, SelectionDomain.UI_ELEMENT}
        ),
        adapter_factory=bind,
    )


def deselect_only_panel_interaction(
    selection_domain: Optional[SelectionDomain] = None,
) -> PanelInteractionDescriptor:
    def bind(_panel: object) -> PanelCommandAdapter:
        return PanelCommandAdapter(
            {
                "edit.deselect": BoundPanelCommand(
                    lambda context: _deselect(context, "panel_deselect"),
                    _can_deselect,
                )
            }
        )

    domains = frozenset({selection_domain}) if selection_domain is not None else frozenset()
    return PanelInteractionDescriptor(
        commands=(PanelCommandSpec("edit.deselect"),),
        shortcuts=(
            PanelShortcutSpec("edit.deselect", KeyChord.parse("Escape")),
        ),
        owned_selection_domains=domains,
        adapter_factory=bind,
    )


def passive_editor_surface_interaction() -> PanelInteractionDescriptor:
    """Describe permanent editor chrome that never owns document focus."""
    return PanelInteractionDescriptor(records_focus_history=False)


def toolbar_panel_interaction() -> PanelInteractionDescriptor:
    """Describe permanent toolbar chrome and its Scene View state ownership.

    The toolbar itself is not an authoring View and must not replace the
    user's active document context. Its camera controls nevertheless create
    undoable View Commands, whose stable semantic owner is Scene View.
    """
    return PanelInteractionDescriptor(
        records_focus_history=False,
        view_command_target_id="scene_view",
    )


def console_panel_interaction(
    view_commands: Optional[ViewCommandService] = None,
) -> PanelInteractionDescriptor:
    view_state = view_commands or ViewCommandService.require()

    def bind(panel: object) -> PanelCommandAdapter:
        _require_methods(
            panel,
            (
                "has_selected_entry",
                "copy_selected_entry",
                "clear",
                "get_info_count",
                "get_warning_count",
                "get_error_count",
                "has_view_option",
                "get_view_option",
                "set_view_option",
                "get_search_query",
                "set_search_query",
                "get_detail_height",
                "set_detail_height",
            ),
            "console panel",
        )

        def _has_entries(_context) -> bool:
            return any(
                int(getter() or 0) > 0
                for getter in (
                    panel.get_info_count,
                    panel.get_warning_count,
                    panel.get_error_count,
                )
            )

        def _clear(_context) -> bool:
            panel.clear()
            return True

        def _option_args(context: CommandContext):
            option = str(context.payload.get("option", "") or "").strip()
            enabled = context.payload.get("enabled")
            if not option or not isinstance(enabled, bool):
                return None
            if not panel.has_view_option(option):
                return None
            return option, enabled

        def _can_set_option(context: CommandContext) -> bool:
            args = _option_args(context)
            return bool(
                args is not None
                and bool(panel.get_view_option(args[0])) != args[1]
            )

        def _set_option(context: CommandContext) -> bool:
            args = _option_args(context)
            if args is None:
                return False
            option, enabled = args
            descriptions = {
                "show_info": "Show Console Logs",
                "show_warnings": "Show Console Warnings",
                "show_errors": "Show Console Errors",
                "collapse": "Collapse Console Entries",
                "clear_on_play": "Set Console Clear on Play",
                "error_pause": "Set Console Error Pause",
                "follow": "Set Console Follow",
            }
            accepted = view_state.set_value(
                bool(panel.get_view_option(option)),
                enabled,
                lambda value: panel.set_view_option(option, bool(value)),
                description=descriptions[option],
            )
            if (
                accepted
                and option == "follow"
                and enabled
                and context.selection.domain is SelectionDomain.DIAGNOSTIC_ENTRY
                and context.selection.owner_id == "console"
            ):
                _deselect(context, "console_follow")
            return accepted

        def _search_args(context: CommandContext):
            old_value = context.payload.get("old_value")
            new_value = context.payload.get("new_value")
            if not isinstance(old_value, str) or not isinstance(new_value, str):
                return None
            return old_value, new_value

        def _set_search(context: CommandContext) -> bool:
            args = _search_args(context)
            if args is None:
                return False
            return view_state.set_value(
                args[0],
                args[1],
                panel.set_search_query,
                description="Search Console",
            )

        def _detail_args(context: CommandContext):
            try:
                old_value = float(context.payload.get("old_value"))
                new_value = float(context.payload.get("new_value"))
            except (TypeError, ValueError):
                return None
            if not math.isfinite(old_value) or not math.isfinite(new_value):
                return None
            return max(40.0, old_value), max(40.0, new_value)

        def _set_detail_height(context: CommandContext) -> bool:
            args = _detail_args(context)
            if args is None:
                return False
            return view_state.set_value(
                args[0],
                args[1],
                panel.set_detail_height,
                description="Resize Console Details",
            )

        return PanelCommandAdapter(
            {
                "console.clear": BoundPanelCommand(_clear, _has_entries),
                "console.set_option": BoundPanelCommand(
                    _set_option,
                    _can_set_option,
                ),
                "console.set_search": BoundPanelCommand(
                    _set_search,
                    lambda context: bool(
                        (args := _search_args(context)) is not None
                        and args[0] != args[1]
                    ),
                ),
                "console.set_detail_height": BoundPanelCommand(
                    _set_detail_height,
                    lambda context: bool(
                        (args := _detail_args(context)) is not None
                        and abs(args[0] - args[1]) > 0.5
                    ),
                ),
                "edit.copy": BoundPanelCommand(
                    lambda _context: panel.copy_selected_entry(),
                    lambda _context: panel.has_selected_entry(),
                ),
                "edit.deselect": BoundPanelCommand(
                    lambda context: _deselect(context, "console_deselect"),
                    _can_deselect,
                ),
            }
        )

    return PanelInteractionDescriptor(
        commands=(
            PanelCommandSpec("console.clear"),
            PanelCommandSpec("console.set_option"),
            PanelCommandSpec("console.set_search"),
            PanelCommandSpec("console.set_detail_height"),
            PanelCommandSpec("edit.copy"),
            PanelCommandSpec("edit.deselect"),
        ),
        shortcuts=(
            PanelShortcutSpec("edit.copy", KeyChord.parse("Ctrl+C")),
            PanelShortcutSpec("edit.deselect", KeyChord.parse("Escape")),
        ),
        owned_selection_domains=frozenset({SelectionDomain.DIAGNOSTIC_ENTRY}),
        adapter_factory=bind,
    )
