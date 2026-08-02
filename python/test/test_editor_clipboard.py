from __future__ import annotations

from Infernux.engine.interaction import (
    ClipboardDomain,
    ClipboardItem,
    ClipboardOperation,
    ClipboardPayload,
    ClipboardService,
    EditorInteractionCore,
)


def test_clipboard_has_one_active_typed_payload():
    clipboard = ClipboardService()
    clipboard.write(
        ClipboardDomain.SCENE_OBJECT,
        (ClipboardItem("42", data={"name": "Cube"}),),
        source_owner_id="hierarchy",
    )

    asset = clipboard.write(
        ClipboardDomain.ASSET,
        (ClipboardItem("C:/Project/Assets/Test.mat"),),
        source_owner_id="project",
    )

    assert clipboard.peek(ClipboardDomain.SCENE_OBJECT) is None
    assert clipboard.peek(ClipboardDomain.ASSET) == asset


def test_clipboard_defensively_copies_domain_data():
    clipboard = ClipboardService()
    document = {"components": [{"speed": 1.0}]}
    payload = clipboard.publish(
        ClipboardPayload(
            ClipboardDomain.SCENE_OBJECT,
            ClipboardOperation.COPY,
            (ClipboardItem("7", data=document),),
        )
    )
    document["components"][0]["speed"] = 9.0
    payload.items[0].data["components"][0]["speed"] = 5.0

    stored = clipboard.peek(ClipboardDomain.SCENE_OBJECT)
    assert stored is not None
    assert stored.items[0].data["components"][0]["speed"] == 1.0


def test_cut_consumption_is_revision_guarded():
    clipboard = ClipboardService()
    old_cut = clipboard.write(
        ClipboardDomain.ASSET,
        (ClipboardItem("C:/Project/Assets/Old.mat"),),
        operation=ClipboardOperation.CUT,
    )
    new_cut = clipboard.write(
        ClipboardDomain.ASSET,
        (ClipboardItem("C:/Project/Assets/New.mat"),),
        operation=ClipboardOperation.CUT,
    )

    assert not clipboard.consume_cut(old_cut.revision)
    assert clipboard.peek(ClipboardDomain.ASSET) == new_cut
    assert clipboard.consume_cut(new_cut.revision)
    assert clipboard.peek() is None


def test_copy_payload_is_not_consumed_by_paste_completion():
    clipboard = ClipboardService()
    copied = clipboard.write(
        ClipboardDomain.ASSET,
        (ClipboardItem("C:/Project/Assets/Test.mat"),),
    )

    assert not clipboard.consume_cut(copied.revision)
    assert clipboard.peek() == copied


def test_interaction_core_owns_clipboard_lifetime():
    core = EditorInteractionCore()
    core.clipboard.write(
        ClipboardDomain.ASSET,
        (ClipboardItem("C:/Project/Assets/Test.mat"),),
    )

    core.shutdown()

    assert core.clipboard.peek() is None
    assert EditorInteractionCore.instance() is None
