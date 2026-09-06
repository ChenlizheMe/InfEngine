from types import SimpleNamespace

import pytest

from Infernux.core.asset_types import TextureType
from Infernux.engine.ui.plugin_panel import PluginPanel
import Infernux.engine.ui.plugin_panel as panel_module


@pytest.mark.parametrize("pages", [[], [{"id": "usage", "title": "Usage"}, {"id": "notes", "title": "Notes"}]])
def test_switching_plugins_selects_first_page_without_locking_it(pages):
    panel = PluginPanel()
    calls = []
    ctx = SimpleNamespace(
        begin_tab_bar=lambda name: True,
        end_tab_bar=lambda: None,
        begin_tab_item=lambda title, selected=False: calls.append((title, selected)) or False,
    )
    manager = SimpleNamespace(content_pages=lambda row: pages)

    def render(reference):
        calls.clear()
        panel._render_detail_pages(ctx, manager, {"reference": reference, "_installed": True}, None)
        return [selected for title, selected in calls]

    assert render("vendor/first") == [True] + [False] * max(1, len(pages))
    assert render("vendor/first") == [False] * (max(1, len(pages)) + 1)
    assert render("vendor/second")[0] is True
    assert render("vendor/first")[0] is True


def test_document_images_request_ui_color_and_full_page_resolution(monkeypatch):
    panel = PluginPanel()
    requests = []
    monkeypatch.setattr(panel_module, "_metric", lambda ctx, value: value)
    monkeypatch.setattr(panel_module, "render_resource_preview_rect", lambda *args, **kwargs: requests.append(kwargs) or True)
    ctx = SimpleNamespace(get_content_region_avail_width=lambda: 900)
    manager = SimpleNamespace(content_asset_path=lambda *args: "/downloaded/plugin_pages/overview.png")
    panel._render_markdown_image(ctx, manager, {}, {}, {"source": "overview.png"})
    settings = requests[0]["texture_settings"]
    assert settings.texture_type == TextureType.UI
    assert settings.srgb is True
    assert settings.max_size >= 720
    assert requests[0]["preserve_aspect"] is True
