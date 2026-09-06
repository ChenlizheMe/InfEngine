from __future__ import annotations

import pytest

from Infernux.engine.ui import _dialogs


def test_sdl_file_filters_translate_tk_wildcards():
    assert _dialogs._sdl_file_filters(
        [
            ("Images", "*.png *.jpg;*.jpeg"),
            ("Scenes", (".scene", "*.prefab")),
            ("Everything", "*.*"),
        ]
    ) == [
        ("Images", "png;jpg;jpeg"),
        ("Scenes", "scene;prefab"),
        ("Everything", "*"),
    ]


def test_linux_open_file_uses_sdl_native_bridge(monkeypatch, tmp_path):
    calls = []

    def fake_dialog(kind, **options):
        calls.append((kind, options))
        return str(tmp_path / "plugin.inxpkg")

    monkeypatch.setattr(_dialogs.sys, "platform", "linux")
    monkeypatch.setattr(_dialogs, "_run_sdl_file_dialog", fake_dialog)
    monkeypatch.chdir(tmp_path)

    selected = _dialogs.pick_file_dialog(
        "Import Package",
        tk_filetypes=[("Infernux Package", "*.inxpkg")],
    )

    assert selected == str(tmp_path / "plugin.inxpkg")
    assert calls == [
        (
            "open_file",
            {
                "title": "Import Package",
                "default_location": str(tmp_path),
                "tk_filetypes": [("Infernux Package", "*.inxpkg")],
            },
        )
    ]


def test_linux_save_file_uses_initial_filename_and_adds_extension(
    monkeypatch, tmp_path
):
    calls = []

    def fake_dialog(kind, **options):
        calls.append((kind, options))
        return str(tmp_path / "New Scene")

    monkeypatch.setattr(_dialogs.sys, "platform", "linux")
    monkeypatch.setattr(_dialogs, "_run_sdl_file_dialog", fake_dialog)

    selected = _dialogs.save_file_dialog(
        title="Save Scene",
        initial_dir=str(tmp_path),
        default_filename="New Scene",
        default_ext="scene",
        tk_filetypes=[("Scene", "*.scene")],
    )

    assert selected == str(tmp_path / "New Scene.scene")
    assert calls == [
        (
            "save_file",
            {
                "title": "Save Scene",
                "default_location": str(tmp_path / "New Scene"),
                "tk_filetypes": [("Scene", "*.scene")],
            },
        )
    ]


def test_sdl_native_dialog_preserves_cancel_but_raises_platform_error(monkeypatch):
    class _Native:
        @staticmethod
        def _show_native_file_dialog(*_args):
            return {"accepted": False, "cancelled": True, "path": "", "error": ""}

    monkeypatch.setattr("Infernux.lib._Infernux", _Native)
    assert _dialogs._run_sdl_file_dialog("open_file", title="Open") is None

    def fail(*_args):
        return {
            "accepted": False,
            "cancelled": False,
            "path": "",
            "error": "portal unavailable",
        }

    monkeypatch.setattr(_Native, "_show_native_file_dialog", fail)
    with pytest.raises(RuntimeError, match="portal unavailable"):
        _dialogs._run_sdl_file_dialog("open_file", title="Open")


def test_sdl_native_dialog_rejects_ambiguous_result(monkeypatch):
    class _Native:
        @staticmethod
        def _show_native_file_dialog(*_args):
            return {"accepted": False, "cancelled": False, "path": "", "error": ""}

    monkeypatch.setattr("Infernux.lib._Infernux", _Native)
    with pytest.raises(RuntimeError, match="invalid result"):
        _dialogs._run_sdl_file_dialog("save_file", title="Save")


def test_windows_open_file_always_uses_native_dialog(monkeypatch):
    calls = []
    monkeypatch.setattr(_dialogs.sys, "platform", "win32")
    monkeypatch.setattr(
        _dialogs,
        "_win32_pick_file",
        lambda title, file_filter: calls.append((title, file_filter)) or "selected.txt",
    )

    assert _dialogs.pick_file_dialog("Open") == "selected.txt"
    assert calls == [("Open", "All files (*.*)\0*.*\0\0")]


@pytest.mark.parametrize(
    ("entry_point", "native_name", "arguments"),
    [
        ("pick_folder_dialog", "_win32_pick_folder", ("Folder",)),
        ("pick_file_dialog", "_win32_pick_file", ("Open", "Text\0*.txt\0\0")),
        (
            "save_file_dialog",
            "_win32_save_file",
            ("Save", "Text\0*.txt\0\0", "C:/project", "file", "txt"),
        ),
    ],
)
def test_windows_native_dialog_errors_propagate(
    monkeypatch, entry_point: str, native_name: str, arguments: tuple[str, ...]
):
    def fail(*_args):
        raise OSError("native dialog unavailable")

    monkeypatch.setattr(_dialogs.sys, "platform", "win32")
    monkeypatch.setattr(_dialogs, native_name, fail)

    with pytest.raises(OSError, match="native dialog unavailable"):
        if entry_point == "pick_folder_dialog":
            _dialogs.pick_folder_dialog(arguments[0])
        elif entry_point == "pick_file_dialog":
            _dialogs.pick_file_dialog(arguments[0], win32_filter=arguments[1])
        else:
            _dialogs.save_file_dialog(
                title=arguments[0],
                win32_filter=arguments[1],
                initial_dir=arguments[2],
                default_filename=arguments[3],
                default_ext=arguments[4],
            )
