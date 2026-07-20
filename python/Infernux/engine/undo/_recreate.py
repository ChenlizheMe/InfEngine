"""Game object recreation from typed documents for structural undo."""

from __future__ import annotations

from typing import Optional

from Infernux.debug import Debug
from Infernux.engine.undo._helpers import _get_active_scene


def _recreate_game_object_from_document(document: dict,
                                        parent_id: Optional[int],
                                        sibling_index: int) -> object:
    scene = _get_active_scene()
    if not scene:
        return None

    from Infernux.engine.component_restore import (
        commit_prepared_game_object_document,
        preflight_game_object_python_components,
    )
    from Infernux.engine.scene_manager import SceneFileManager
    sfm = SceneFileManager.instance()
    prepared = preflight_game_object_python_components(
        document,
        asset_database=sfm._asset_database if sfm else None,
        preserve_document_ids=True,
        reference_scene=scene,
    )

    obj = scene.create_game_object("__undo_restore__")
    if not obj:
        prepared.discard()
        return None

    if not commit_prepared_game_object_document(obj, document, prepared):
        scene.destroy_game_object(obj)
        scene.process_pending_destroys()
        return None

    if parent_id is not None:
        parent = scene.find_by_id(parent_id)
        if parent:
            obj.set_parent(parent)

    if getattr(obj, "transform", None):
        obj.transform.set_sibling_index(sibling_index)

    scene.awake_object(obj)
    return obj
