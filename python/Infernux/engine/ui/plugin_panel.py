"""Editor surfaces for plugin discovery and selective InxPackage import."""

from __future__ import annotations

from typing import Mapping

from Infernux.core.asset_types import TextureImportSettings, TextureType
from Infernux.engine.i18n import get_locale, t
from Infernux.engine.interaction import PanelInteractionDescriptor
from Infernux.engine.path_utils import resolved_path
from Infernux.plugins import (
    InxPackage,
    PluginManager,
    localized_intro,
    markdown_to_plain_text,
    parse_markdown_blocks,
    plugin_install_block_reason,
)

from .asset_resource_preview import render_resource_preview_rect
from .dpi import scaled_editor_metric
from .editor_panel import EditorPanel
from .panel_registry import editor_panel
from .theme import ImGuiCol, ImGuiStyleVar, Theme


def _metric(ctx, value: float) -> float:
    return scaled_editor_metric(ctx, value)


@editor_panel(
    "Plugins",
    type_id="plugins",
    title_key="panel.plugins",
    menu_path="Extensions",
    interaction=PanelInteractionDescriptor(),
)
class PluginPanel(EditorPanel):
    """Infernux-native package browser for the project InxPackage registry."""

    def __init__(self) -> None:
        super().__init__(t("panel.plugins"), "plugins")
        self._search = ""
        self._source = ""
        self._pip = ""
        self._message = ""
        self._selected_reference = ""
        self._detail_reference = ""
        self._document_texture_settings = TextureImportSettings(texture_type=TextureType.UI)
        self._scope_index = 0
        self._sort_index = 2
        from .plugin_versions import PluginVersionsView

        self._versions = PluginVersionsView()

    def _initial_size(self) -> tuple[float, float]:
        return 1180.0, 740.0

    def select_reference(self, reference: str) -> bool:
        """Reveal one package after navigation from another editor surface."""

        value = str(reference or "").strip()
        if not value:
            return False
        self._scope_index = 0
        self._search = ""
        self._selected_reference = value
        return True

    def _push_window_style(self, ctx) -> None:
        ctx.push_style_var_vec2(
            ImGuiStyleVar.WindowPadding,
            _metric(ctx, Theme.PROJECT_PANEL_PAD[0]),
            _metric(ctx, Theme.PROJECT_PANEL_PAD[1]),
        )
        ctx.push_style_var_vec2(
            ImGuiStyleVar.ItemSpacing,
            _metric(ctx, Theme.TOOLBAR_ITEM_SPC[0]),
            _metric(ctx, Theme.TOOLBAR_ITEM_SPC[1]),
        )

    def _pop_window_style(self, ctx) -> None:
        ctx.pop_style_var(2)

    def on_render_content(self, ctx) -> None:
        manager = PluginManager.instance()
        if manager is None:
            ctx.text_wrapped(t("plugins.unavailable"))
            return
        self._render_browser(ctx, manager)

    def _render_browser(self, ctx, manager: PluginManager) -> None:
        self._render_toolbar(ctx, manager)
        rows = self._visible_rows(manager)
        keys = {str(row.get("reference", "")).casefold() for row in rows}
        if self._selected_reference.casefold() not in keys:
            preferred = next(
                (
                    row for row in rows
                    if (
                        (state := manager.states.get(str(row.get("reference", "")).casefold()))
                        and state.loaded
                    )
                ),
                rows[0] if rows else None,
            )
            self._selected_reference = str(preferred.get("reference", "")) if preferred else ""

        ctx.dummy(0.0, _metric(ctx, Theme.INSPECTOR_SECTION_GAP))
        ctx.separator()
        ctx.dummy(0.0, _metric(ctx, Theme.INSPECTOR_SECTION_GAP))
        available_w = max(_metric(ctx, 520.0), ctx.get_content_region_avail_width())
        available_h = max(_metric(ctx, 260.0), ctx.get_content_region_avail_height())
        column_gap = _metric(ctx, Theme.INSPECTOR_TITLE_GAP)
        left_w = min(_metric(ctx, 360.0), max(_metric(ctx, 260.0), available_w * 0.31))
        right_w = max(_metric(ctx, 240.0), available_w - left_w - column_gap)

        if ctx.begin_child("##plugins_package_list", left_w, available_h, True, Theme.WINDOW_FLAGS_NO_SCROLL):
            self._render_package_list(ctx, manager, rows)
        ctx.end_child()
        ctx.same_line(0.0, column_gap)
        if ctx.begin_child("##plugins_package_details", right_w, available_h, False, Theme.WINDOW_FLAGS_NO_SCROLL):
            selected = next(
                (
                    row for row in rows
                    if str(row.get("reference", "")).casefold() == self._selected_reference.casefold()
                ),
                None,
            )
            self._render_package_details(ctx, manager, selected)
        ctx.end_child()

    def _render_toolbar(self, ctx, manager: PluginManager) -> None:
        if ctx.button(t("plugins.add") + "##plugins_add", width=_metric(ctx, 112.0)):
            ctx.open_popup("##plugins_add_popup")
        ctx.same_line()
        scopes = [
            t("plugins.scope.all"),
            t("plugins.scope.project"),
            t("plugins.scope.registry"),
            t("plugins.scope.downloaded"),
        ]
        ctx.set_next_item_width(_metric(ctx, 160.0))
        self._scope_index = ctx.combo("##plugins_scope", self._scope_index, scopes)
        ctx.same_line()
        sorts = [
            t("plugins.sort.name_asc"),
            t("plugins.sort.name_desc"),
            t("plugins.sort.status"),
        ]
        ctx.set_next_item_width(_metric(ctx, 152.0))
        self._sort_index = ctx.combo("##plugins_sort", self._sort_index, sorts)
        ctx.same_line()
        if ctx.button(t("plugins.refresh_catalog") + "##plugins_refresh_catalog"):
            self._refresh_catalog(manager)
        ctx.dummy(0.0, _metric(ctx, Theme.INSPECTOR_SECTION_GAP))
        ctx.set_next_item_width(-1.0)
        self._search = ctx.input_text_with_hint(
            "##plugin_search",
            t("plugins.search"),
            self._search,
            512,
        )
        if manager.official_catalog_error:
            ctx.dummy(0.0, _metric(ctx, Theme.INSPECTOR_SECTION_GAP))
            ctx.push_style_color(ImGuiCol.Text, *Theme.ERROR_TEXT)
            ctx.text_wrapped(t("plugins.official_unavailable"))
            ctx.pop_style_color()
        self._render_add_popup(ctx, manager)

    def _render_add_popup(self, ctx, manager: PluginManager) -> None:
        if not ctx.begin_popup("##plugins_add_popup"):
            return
        ctx.label(t("plugins.add_source_title"))
        ctx.text_wrapped(t("plugins.add_source_help"))
        ctx.set_next_item_width(_metric(ctx, 430.0))
        self._source = ctx.input_text_with_hint(
            "##plugin_source",
            t("plugins.source"),
            self._source,
            2048,
        )
        if ctx.button(
            t("plugins.install_source") + "##plugin_install_source",
            width=_metric(ctx, 150.0),
        ):
            self._request_install(manager, "source", self._source)
            ctx.close_current_popup()
        ctx.separator()
        ctx.label(t("plugins.python_dependencies"))
        ctx.text_wrapped(t("plugins.pip_not_plugin"))
        ctx.set_next_item_width(_metric(ctx, 430.0))
        self._pip = ctx.input_text_with_hint(
            "##plugin_pip",
            t("plugins.pip_syntax"),
            self._pip,
            4096,
        )
        if ctx.button(
            t("plugins.run_pip") + "##plugin_run_pip",
            width=_metric(ctx, 150.0),
        ):
            self._request_install(manager, "pip", self._pip)
            ctx.close_current_popup()
        if self._message:
            ctx.separator()
            ctx.text_wrapped(self._message)
        ctx.end_popup()

    def _visible_rows(self, manager: PluginManager) -> list[dict[str, object]]:
        installed = {
            str(item.get("reference", "")).casefold(): dict(item)
            for item in manager.registry.installed()
        }
        rows: list[dict[str, object]] = []
        known: set[str] = set()
        for value in manager.registry.available():
            row = dict(value)
            key = str(row.get("reference", "")).casefold()
            row["_registry"] = True
            source = row.get("source", {})
            row["_official"] = bool(
                isinstance(source, Mapping) and source.get("official", False)
            )
            row["_cached"] = bool(
                manager.cached_reference_path(str(row.get("reference", "")))
            )
            row["_installed"] = key in installed
            row["_install_block_reason"] = (
                ""
                if row["_installed"]
                else plugin_install_block_reason(str(row.get("reference", "")))
            )
            if key in installed:
                installed_version = str(installed[key].get("version", "")).strip()
                merged = dict(row)
                merged.update(installed[key])
                merged["_registry"] = True
                merged["_installed"] = True
                merged["_installed_version"] = installed_version
                merged["_official"] = row["_official"]
                merged["_cached"] = row["_cached"]
                merged["_install_block_reason"] = ""
                row = merged
            rows.append(row)
            known.add(key)
        for key, value in installed.items():
            if key in known:
                continue
            value["_registry"] = False
            value["_installed"] = True
            value["_official"] = False
            value["_cached"] = False
            value["_install_block_reason"] = ""
            value["_installed_version"] = str(value.get("version", "")).strip()
            rows.append(value)

        query = self._search.strip().casefold()
        filtered = []
        for row in rows:
            if self._scope_index == 1 and not row.get("_installed"):
                continue
            if self._scope_index == 2 and not row.get("_registry"):
                continue
            if self._scope_index == 3 and not row.get("_cached"):
                continue
            localized_intros = row.get("intros", {})
            intro_values = (
                localized_intros.values()
                if isinstance(localized_intros, Mapping) else ()
            )
            haystack = " ".join(
                [
                    *(str(row.get(field, "")) for field in ("name", "reference", "intro", "version")),
                    *(str(value) for value in intro_values),
                ]
            ).casefold()
            if query and query not in haystack:
                continue
            filtered.append(row)
        name_key = lambda row: (
            str(row.get("name") or row.get("reference", "")).casefold(),
            str(row.get("reference", "")).casefold(),
        )
        if self._sort_index == 2:
            filtered.sort(
                key=lambda row: (
                    self._status_rank(
                        manager.states.get(str(row.get("reference", "")).casefold()),
                        row,
                    ),
                    *name_key(row),
                )
            )
        else:
            filtered.sort(key=name_key, reverse=self._sort_index == 1)
        return filtered

    def _render_package_list(self, ctx, manager: PluginManager, rows: list[dict[str, object]]) -> None:
        ctx.push_style_color(ImGuiCol.Text, *Theme.TEXT_DIM)
        ctx.label(t("plugins.packages_count").format(count=len(rows)))
        ctx.pop_style_color()
        ctx.separator()
        ctx.dummy(0.0, _metric(ctx, Theme.INSPECTOR_SECTION_GAP))
        footer_h = _metric(ctx, 42.0)
        section_gap = _metric(ctx, Theme.INSPECTOR_SECTION_GAP)
        body_y = ctx.get_cursor_pos_y()
        body_h = ctx.get_content_region_avail_height()
        footer_y = body_y + max(0.0, body_h - footer_h)
        rows_h = max(_metric(ctx, 40.0), body_h - footer_h - section_gap)
        ctx.push_style_var_vec2(
            ImGuiStyleVar.WindowPadding,
            _metric(ctx, Theme.INSPECTOR_LIST_BODY_PAD_X),
            _metric(ctx, Theme.INSPECTOR_LIST_BODY_PAD_Y),
        )
        rows_visible = ctx.begin_child("##plugin_rows", 0.0, rows_h, False)
        if rows_visible:
            if not rows:
                ctx.text_wrapped(t("plugins.empty"))
            else:
                ctx.push_style_var_vec2(
                    ImGuiStyleVar.ItemSpacing,
                    _metric(ctx, Theme.TREE_ITEM_SPC[0]),
                    _metric(ctx, Theme.TREE_ITEM_SPC[1]),
                )
                for row in rows:
                    reference = str(row.get("reference", ""))
                    key = reference.casefold()
                    state = manager.states.get(key)
                    status, status_color = self._state_visual(state, row)
                    name = str(row.get("name") or reference)
                    if row.get("_official"):
                        name = f"{name}  [{t('plugins.official')}]"
                    selected = key == self._selected_reference.casefold()
                    if ctx.selectable(
                        f"##plugin_row_{key}", selected, 0, 0.0, _metric(ctx, 26.0)
                    ):
                        self._selected_reference = reference
                    x0 = ctx.get_item_rect_min_x()
                    y0 = ctx.get_item_rect_min_y()
                    x1 = ctx.get_item_rect_max_x()
                    y1 = ctx.get_item_rect_max_y()
                    status_w = ctx.calc_text_width(status)
                    name_right = max(
                        x0 + _metric(ctx, 48.0),
                        x1
                        - status_w
                        - _metric(ctx, Theme.INSPECTOR_TITLE_GAP)
                        - _metric(ctx, 10.0),
                    )
                    display_name = self._fit_text(
                        ctx,
                        name,
                        max(_metric(ctx, 24.0), name_right - x0 - _metric(ctx, 10.0)),
                    )
                    ctx.draw_text_aligned(
                        x0 + _metric(ctx, 8.0), y0, name_right, y1,
                        display_name, *Theme.TEXT, 0.0, 0.5, 0.0, True,
                    )
                    ctx.draw_text_aligned(
                        name_right, y0, x1 - _metric(ctx, 8.0), y1,
                        status, *status_color, 1.0, 0.5, 0.0, True,
                    )
                ctx.pop_style_var(1)
        ctx.end_child()
        ctx.pop_style_var(1)

        ctx.set_cursor_pos_y(footer_y)
        ctx.push_style_color(ImGuiCol.ChildBg, *Theme.FRAME_BG)
        footer_visible = ctx.begin_child("##plugin_list_footer", 0.0, footer_h, True)
        if footer_visible:
            if ctx.button(
                t("plugins.refresh") + "##plugin_refresh", width=_metric(ctx, 80.0)
            ):
                references = tuple(
                    str(item.get("reference", ""))
                    for item in manager.registry.installed()
                    if item.get("enabled", True)
                )
                self._begin_reload(manager, references)
            ctx.same_line(0.0, _metric(ctx, Theme.INSPECTOR_TITLE_GAP))
            ctx.push_style_color(ImGuiCol.Text, *Theme.TEXT_DIM)
            ctx.label(
                t("plugins.stats").format(
                    available=len(manager.registry.available()),
                    installed=len(manager.registry.installed()),
                )
            )
            ctx.pop_style_color()
        ctx.end_child()
        ctx.pop_style_color()

    def _render_package_details(
        self,
        ctx,
        manager: PluginManager,
        row: Mapping[str, object] | None,
    ) -> None:
        if row is None:
            ctx.text_wrapped(t("plugins.select_package"))
            return
        reference = str(row.get("reference", ""))
        key = reference.casefold()
        state = manager.states.get(key)
        installed = bool(row.get("_installed"))
        name = str(row.get("name") or reference)
        version = str(row.get("_installed_version") or row.get("version", ""))
        status, status_color = self._state_visual(state, row)

        summary_h = _metric(ctx, 72.0)
        footer_h = _metric(ctx, 46.0)
        section_gap = _metric(ctx, Theme.INSPECTOR_SECTION_GAP)
        details_y = ctx.get_cursor_pos_y()
        details_h = ctx.get_content_region_avail_height()
        content_y = details_y + summary_h + section_gap
        footer_y = details_y + max(0.0, details_h - footer_h)
        content_h = max(_metric(ctx, 40.0), footer_y - section_gap - content_y)
        self._render_detail_summary(ctx, name, reference, version, status, status_color, summary_h)

        ctx.set_cursor_pos_y(content_y)
        ctx.push_style_color(ImGuiCol.ChildBg, *Theme.WINDOW_BG)
        content_visible = ctx.begin_child("##plugin_detail_content", 0.0, content_h, True)
        if content_visible:
            self._render_detail_pages(ctx, manager, row, state)
        ctx.end_child()
        ctx.pop_style_color()

        ctx.set_cursor_pos_y(footer_y)
        self._render_detail_footer(ctx, manager, row, reference, key, installed, footer_h)

    @staticmethod
    def _render_detail_summary(ctx, name, reference, version, status, status_color, height) -> None:
        ctx.push_style_color(ImGuiCol.ChildBg, *Theme.FRAME_BG)
        visible = ctx.begin_child(
            "##plugin_summary", 0.0, height, True, Theme.WINDOW_FLAGS_NO_SCROLL
        )
        if visible:
            start_x = ctx.get_cursor_pos_x()
            width = ctx.get_content_region_avail_width()
            status_x = start_x + max(
                _metric(ctx, 80.0), width - ctx.calc_text_width(status)
            )
            ctx.label(name)
            ctx.same_line(status_x)
            ctx.push_style_color(ImGuiCol.Text, *status_color)
            ctx.label(status)
            ctx.pop_style_color()
            ctx.push_style_color(ImGuiCol.Text, *Theme.TEXT_DIM)
            ctx.label(reference)
            ctx.same_line(0.0, _metric(ctx, Theme.INSPECTOR_TITLE_GAP))
            ctx.label(version or "-")
            ctx.pop_style_color()
        ctx.end_child()
        ctx.pop_style_color()

    def _render_detail_pages(self, ctx, manager, row, state) -> None:
        pages = manager.content_pages(row)
        reference = str(row.get("reference", "")).casefold()
        select_first = reference != self._detail_reference
        if ctx.begin_tab_bar("##plugins_details_tabs"):
            self._detail_reference = reference
            if pages:
                for index, page in enumerate(pages):
                    page_id = str(page.get("id", "page"))
                    title = self._page_title(page_id, str(page.get("title", page_id)))
                    if ctx.begin_tab_item(f"{title}##plugin_page_{page_id}", selected=select_first and index == 0):
                        content = str(page.get("content", ""))
                        if str(page.get("format", "text")) == "markdown":
                            self._render_markdown_page(ctx, manager, row, page, content)
                        else:
                            ctx.text_wrapped(content or t("plugins.page_empty"))
                        if page_id == "intro":
                            ctx.dummy(0.0, _metric(ctx, Theme.INSPECTOR_TITLE_GAP))
                            self._render_metadata(ctx, row)
                            self._render_diagnostics(ctx, row, state)
                        ctx.end_tab_item()
            else:
                if ctx.begin_tab_item(t("plugins.tab.description"), selected=select_first):
                    ctx.text_wrapped(t("plugins.no_description"))
                    ctx.dummy(0.0, _metric(ctx, Theme.INSPECTOR_TITLE_GAP))
                    self._render_metadata(ctx, row)
                    self._render_diagnostics(ctx, row, state)
                    ctx.end_tab_item()
            if row.get("_installed") and ctx.begin_tab_item(t("plugins.versions.tab")):
                self._versions.render(ctx, manager, row)
                ctx.end_tab_item()
            ctx.end_tab_bar()

    def _render_markdown_page(self, ctx, manager, row, page, content: str) -> None:
        rendered = False
        for index, block in enumerate(parse_markdown_blocks(content)):
            if rendered:
                ctx.dummy(0.0, _metric(ctx, Theme.INSPECTOR_SECTION_GAP))
            kind = str(block.get("kind", "paragraph"))
            text = markdown_to_plain_text(str(block.get("content", "")))
            if kind == "heading":
                level = max(1, min(6, int(block.get("level", 1))))
                color = Theme.PREFAB_TEXT if level == 1 else Theme.TEXT if level <= 3 else Theme.TEXT_DIM
                prefix = "" if level <= 2 else "- " if level == 3 else "-- "
                ctx.push_style_color(ImGuiCol.Text, *color)
                ctx.text_wrapped(prefix + text)
                ctx.pop_style_color()
                if level <= 2:
                    ctx.separator()
            elif kind == "divider":
                ctx.separator()
            elif kind == "list_item":
                marker = str(block.get("marker", "•")) if block.get("ordered") else "•"
                depth = max(0, min(6, int(block.get("depth", 0))))
                ctx.text_wrapped(f"{'    ' * depth}{marker}  {text}")
            elif kind == "quote":
                ctx.push_style_color(ImGuiCol.Text, *Theme.TEXT_DIM)
                ctx.text_wrapped(f"│  {text}")
                ctx.pop_style_color()
            elif kind == "code":
                lines = str(block.get("content", "")).count("\n") + 1
                language_h = (
                    _metric(ctx, 22.0)
                    if str(block.get("language", "")).strip()
                    else 0.0
                )
                height = max(
                    _metric(ctx, 42.0),
                    min(
                        _metric(ctx, 280.0),
                        _metric(ctx, 30.0) + language_h + lines * _metric(ctx, 18.0),
                    ),
                )
                ctx.push_style_color(ImGuiCol.ChildBg, *Theme.FRAME_BG)
                visible = ctx.begin_child(
                    f"##plugin_markdown_code_{index}",
                    0.0,
                    height,
                    True,
                    Theme.WINDOW_FLAGS_NO_SCROLL,
                )
                if visible:
                    language = str(block.get("language", "")).strip()
                    if language:
                        ctx.push_style_color(ImGuiCol.Text, *Theme.TEXT_DIM)
                        ctx.label(language)
                        ctx.pop_style_color()
                    ctx.text_wrapped(str(block.get("content", "")))
                ctx.end_child()
                ctx.pop_style_color()
            elif kind == "image":
                self._render_markdown_image(ctx, manager, row, page, block)
            elif text:
                ctx.text_wrapped(text)
            rendered = True
        if not rendered:
            ctx.text_wrapped(t("plugins.page_empty"))

    def _render_markdown_image(self, ctx, manager, row, page, block) -> None:
        source = str(block.get("source", ""))
        image_path = manager.content_asset_path(row, page, source)
        width = min(
            _metric(ctx, 720.0),
            max(_metric(ctx, 1.0), ctx.get_content_region_avail_width()),
        )
        shown = bool(image_path) and render_resource_preview_rect(
            ctx,
            self,
            image_path,
            width,
            min(_metric(ctx, 360.0), width),
            preserve_aspect=True,
            texture_settings=self._document_texture_settings,
        )
        if not shown:
            label = str(block.get("alt", "")).strip() or source
            ctx.push_style_color(ImGuiCol.Text, *Theme.TEXT_DIM)
            ctx.label(label)
            ctx.pop_style_color()

    @staticmethod
    def _page_title(page_id: str, fallback: str) -> str:
        if page_id == "intro":
            return t("plugins.tab.description")
        if page_id == "license":
            return t("plugins.tab.license")
        return fallback

    @staticmethod
    def _render_diagnostics(ctx, row, state) -> None:
        diagnostic = str(row.get("diagnostic", "")).strip()
        if diagnostic:
            ctx.separator()
            ctx.push_style_color(ImGuiCol.Text, *Theme.ERROR_TEXT)
            ctx.text_wrapped(diagnostic)
            ctx.pop_style_color()
        if state and state.error:
            ctx.separator()
            ctx.push_style_color(ImGuiCol.Text, *Theme.ERROR_TEXT)
            ctx.text_wrapped(state.error)
            ctx.pop_style_color()
        install_block = str(row.get("_install_block_reason", "")).strip()
        if install_block:
            ctx.separator()
            ctx.push_style_color(ImGuiCol.Text, *Theme.ERROR_TEXT)
            ctx.text_wrapped(t(install_block))
            ctx.pop_style_color()

    def _render_detail_footer(self, ctx, manager, row, reference, key, installed, height) -> None:
        source = self._source_text(row)
        location = self._plugin_location(manager, row, source)
        ctx.push_style_color(ImGuiCol.ChildBg, *Theme.FRAME_BG)
        visible = ctx.begin_child("##plugin_detail_footer", 0.0, height, True)
        if visible:
            button_w = _metric(ctx, 104.0)
            footer_start_x = ctx.get_cursor_pos_x()
            footer_w = ctx.get_content_region_avail_width()
            if ctx.button(t("plugins.copy_reference") + "##plugin_copy_reference", width=button_w):
                ctx.set_clipboard_text(reference)
            ctx.same_line()
            if ctx.button(t("plugins.copy_source") + "##plugin_copy_source", width=button_w):
                ctx.set_clipboard_text(source)
            if location:
                ctx.same_line()
                if ctx.button(t("plugins.open_location") + "##plugin_open_location", width=button_w):
                    self._open_plugin_location(location)

            cached = bool(row.get("_cached"))
            action_count = 3 if installed else (2 if cached else 1)
            action_x = footer_start_x + max(
                0.0,
                footer_w
                - action_count * button_w
                - (action_count - 1) * _metric(ctx, Theme.INSPECTOR_TITLE_GAP),
            )
            ctx.same_line(action_x)
            if installed:
                enabled = bool(row.get("enabled", True))
                toggle_label = t("plugins.disable") if enabled else t("plugins.enable")
                if ctx.button(toggle_label + f"##plugin_toggle_{key}", width=button_w):
                    self._run(lambda ref=reference, value=not enabled: manager.set_enabled(ref, value), "toggle")
                ctx.same_line(0.0, _metric(ctx, Theme.INSPECTOR_TITLE_GAP))
                if ctx.button(t("plugins.uninstall") + f"##plugin_uninstall_{key}", width=button_w):
                    self._run(lambda ref=reference: manager.uninstall(ref), "uninstall")
                ctx.same_line(0.0, _metric(ctx, Theme.INSPECTOR_TITLE_GAP))
                if self._primary_button(ctx, t("plugins.reload") + f"##plugin_reload_{key}"):
                    self._begin_reload(manager, (reference,))
            else:
                blocked = bool(str(row.get("diagnostic", "")).strip())
                install_blocked = bool(
                    str(row.get("_install_block_reason", "")).strip()
                )
                if cached:
                    if blocked:
                        ctx.begin_disabled(True)
                    if ctx.button(
                        t("plugins.redownload") + f"##plugin_redownload_{key}",
                        width=button_w,
                    ):
                        self._begin_install(
                            manager,
                            label=reference,
                            work=lambda report, ref=reference: manager.download_reference(
                                ref,
                                force=True,
                                progress=report,
                            ),
                            action="download",
                        )
                    if blocked:
                        ctx.end_disabled()
                    ctx.same_line(0.0, _metric(ctx, Theme.INSPECTOR_TITLE_GAP))
                    if blocked or install_blocked:
                        ctx.begin_disabled(True)
                    if self._primary_button(
                        ctx, t("plugins.import") + f"##plugin_import_{key}"
                    ):
                        self._begin_install(
                            manager,
                            label=reference,
                            work=lambda report, ref=reference: manager.install_reference(
                                ref,
                                progress=report,
                            ),
                            action="install",
                        )
                    if blocked or install_blocked:
                        ctx.end_disabled()
                else:
                    if blocked:
                        ctx.begin_disabled(True)
                    if self._primary_button(
                        ctx, t("plugins.download") + f"##plugin_download_{key}"
                    ):
                        self._begin_install(
                            manager,
                            label=reference,
                            work=lambda report, ref=reference: manager.download_reference(
                                ref,
                                progress=report,
                            ),
                            action="download",
                        )
                    if blocked:
                        ctx.end_disabled()
        ctx.end_child()
        ctx.pop_style_color()

    @staticmethod
    def _source_text(row: Mapping[str, object]) -> str:
        source = row.get("source")
        if isinstance(source, Mapping):
            source_type = str(source.get("type", "")).strip()
            location = str(source.get("location", "")).strip()
            if source_type or location:
                return f"{source_type}: {location}".strip(": ")
        package_path = str(row.get("package_path", "")).strip()
        return package_path or "-"

    def _render_metadata(self, ctx, row: Mapping[str, object]) -> None:
        reference = str(row.get("reference", ""))
        source = self._source_text(row)
        root_value = f"Packages/{reference}" if reference else "-"
        label_x = ctx.get_cursor_pos_x()
        value_x = label_x + _metric(ctx, 112.0)
        value_w = max(
            _metric(ctx, 120.0),
            ctx.get_content_region_avail_width() - _metric(ctx, 112.0),
        )
        for label, value in (
            (t("plugins.detail.reference"), reference),
            (t("plugins.detail.category"), str(row.get("category", "Other"))),
            (
                t("plugins.detail.targets"),
                ", ".join(str(value) for value in row.get("targets", [])) or "-",
            ),
            (t("plugins.detail.source"), source),
            (t("plugins.detail.root"), root_value),
        ):
            ctx.push_style_color(ImGuiCol.Text, *Theme.TEXT_DIM)
            ctx.label(label)
            ctx.pop_style_color()
            ctx.same_line(value_x)
            display = self._fit_text(ctx, value, value_w)
            ctx.label(display)

    @staticmethod
    def _plugin_location(manager: PluginManager, row: Mapping[str, object], source: str) -> str:
        location = str(row.get("package_path", "")).strip()
        if not location and source.startswith(("http://", "https://", "github: http")):
            location = source.removeprefix("github: ")
        reference = str(row.get("reference", "")).strip()
        root_value = f"Packages/{reference}" if reference else "-"
        if not location and root_value not in {"", "-"}:
            location = resolved_path(f"{manager.project_root}/{root_value}")
        return location

    @staticmethod
    def _open_plugin_location(location: str) -> None:
        if location.startswith(("http://", "https://")):
            import webbrowser

            webbrowser.open(location)
        else:
            from .project_utils import reveal_in_file_explorer

            reveal_in_file_explorer(location)

    @staticmethod
    def _primary_button(ctx, label: str) -> bool:
        ctx.push_style_color(ImGuiCol.Button, *Theme.APPLY_BUTTON)
        ctx.push_style_color(ImGuiCol.ButtonHovered, *Theme.PREFAB_BTN_HOVERED)
        ctx.push_style_color(ImGuiCol.ButtonActive, *Theme.PREFAB_BTN_ACTIVE)
        clicked = bool(ctx.button(label, width=_metric(ctx, 104.0)))
        ctx.pop_style_color(3)
        return clicked

    @staticmethod
    def _fit_text(ctx, value: str, max_width: float) -> str:
        if ctx.calc_text_width(value) <= max_width:
            return value
        low, high = 0, len(value)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = f"{value[:middle]}..."
            if ctx.calc_text_width(candidate) <= max_width:
                low = middle
            else:
                high = middle - 1
        return f"{value[:low]}..."

    @classmethod
    def _status_rank(cls, state, row: Mapping[str, object]) -> int:
        label, _color = cls._state_visual(state, row)
        return {
            t("plugins.error"): 0,
            t("plugins.loaded"): 1,
            t("plugins.disabled"): 2,
            t("plugins.installed"): 3,
            t("plugins.downloaded"): 4,
            t("plugins.available"): 5,
            t("plugins.downloadable"): 6,
        }.get(label, 6)

    @staticmethod
    def _state_visual(state, row: Mapping[str, object]) -> tuple[str, tuple]:
        if (state and state.error) or str(row.get("diagnostic", "")).strip():
            return t("plugins.error"), Theme.ERROR_TEXT
        if state and not state.enabled:
            return t("plugins.disabled"), Theme.TEXT_DISABLED
        if state and state.loaded:
            return t("plugins.loaded"), Theme.SUCCESS_TEXT
        if row.get("_installed"):
            return t("plugins.installed"), Theme.SUCCESS_TEXT
        if row.get("_cached"):
            return t("plugins.downloaded"), Theme.SUCCESS_TEXT
        source = row.get("source")
        if isinstance(source, Mapping) and str(source.get("type", "")).casefold() == "local":
            return t("plugins.available"), Theme.SUCCESS_TEXT
        return t("plugins.downloadable"), Theme.TEXT_DIM

    def _refresh_catalog(self, manager: PluginManager) -> None:
        from Infernux.plugins.official import refresh_official_registry
        from .plugin_install_progress import PluginInstallProgressService

        def complete(ok, result, message):
            if ok:
                manager.official_catalog_error = ""
                self._message = t("plugins.catalog_refreshed")
            else:
                self._message = message

        if not PluginInstallProgressService.instance().begin(
            label=t("plugins.refresh_catalog"),
            work=lambda report: refresh_official_registry(manager.project_root),
            complete=complete,
        ):
            self._message = t("plugins.install_progress.busy")

    def _begin_reload(self, manager: PluginManager, references: tuple[str, ...]) -> None:
        if not references:
            self._message = t("plugins.reload_none")
            return
        from .plugin_reload_progress import PluginReloadProgressService

        def complete(ok: bool, _states: tuple[object, ...], message: str) -> None:
            self._message = "" if ok else message or t("plugins.reload_failed")

        if not PluginReloadProgressService.instance().begin(
            manager=manager,
            references=references,
            complete=complete,
        ):
            self._message = t("plugins.reload_busy")

    def _request_install(self, manager: PluginManager, kind: str, value: str) -> None:
        syntax = str(value or "").strip()
        if not syntax:
            self._message = t("plugins.install_confirm.empty")
            return
        from .plugin_install_confirmation import (
            PluginInstallConfirmationCoordinator,
        )

        requested = PluginInstallConfirmationCoordinator.instance().request(
            kind,
            syntax,
            lambda: self._begin_install(
                manager,
                label=syntax,
                work=(
                    (lambda report: manager.install_source(syntax, progress=report))
                    if kind == "source"
                    else (lambda report: manager.install_pip(syntax, progress=report))
                ),
                action=kind,
            ),
        )
        if not requested:
            self._message = t("plugins.install_progress.busy")

    def _begin_install(self, manager: PluginManager, *, label: str, work, action: str) -> None:
        from .plugin_install_progress import PluginInstallProgressService

        def complete(ok: bool, result: object | None, message: str) -> None:
            if not ok:
                self._message = message or t("plugins.install_progress.failed")
                return
            reference = str(getattr(result, "reference", ""))
            if reference:
                try:
                    result = manager.finalize_background_install(reference)
                except Exception as exc:
                    self._message = f"{type(exc).__name__}: {exc}"
                    return
                self._selected_reference = reference
            self._message = t("plugins.action_ok").format(
                action=action,
                reference=reference,
            )

        if not PluginInstallProgressService.instance().begin(
            label=label,
            work=work,
            complete=complete,
        ):
            self._message = t("plugins.install_progress.busy")

    def _run(self, callback, action: str) -> None:
        try:
            result = callback()
            reference = getattr(result, "reference", "")
            if reference:
                self._selected_reference = str(reference)
            elif action == "uninstall":
                self._selected_reference = ""
            self._message = t("plugins.action_ok").format(action=action, reference=reference)
        except Exception as exc:
            self._message = f"{type(exc).__name__}: {exc}"


@editor_panel(
    "Import InxPackage",
    type_id="inxpackage_import",
    title_key="panel.inxpackage_import",
    menu_path="",
    interaction=PanelInteractionDescriptor(),
)
class InxPackageImportPanel(EditorPanel):
    """Dockable package preview with per-entry extraction selection."""

    def __init__(self) -> None:
        super().__init__(t("panel.inxpackage_import"), "inxpackage_import")
        self.package_path = ""
        self._preview = None
        self._selected: dict[str, bool] = {}
        self._install_block_reason = ""
        self._message = ""

    def _initial_size(self) -> tuple[float, float]:
        return 760.0, 560.0

    def _push_window_style(self, ctx) -> None:
        ctx.push_style_var_vec2(
            ImGuiStyleVar.WindowPadding,
            _metric(ctx, Theme.PROJECT_PANEL_PAD[0]),
            _metric(ctx, Theme.PROJECT_PANEL_PAD[1]),
        )
        ctx.push_style_var_vec2(
            ImGuiStyleVar.ItemSpacing,
            _metric(ctx, Theme.TOOLBAR_ITEM_SPC[0]),
            _metric(ctx, Theme.TOOLBAR_ITEM_SPC[1]),
        )

    def _pop_window_style(self, ctx) -> None:
        ctx.pop_style_var(2)

    def open_package(self, package_path: str) -> None:
        self.package_path = resolved_path(package_path)
        self._preview = InxPackage.inspect(self.package_path)
        self._selected = {path: True for path in self._preview.project_entries}
        self._install_block_reason = plugin_install_block_reason(
            str(self._preview.metadata.get("reference", ""))
        )
        self._message = (
            t(self._install_block_reason) if self._install_block_reason else ""
        )

    def _begin_import(self) -> bool:
        manager = PluginManager.instance()
        if manager is None:
            self._message = t("plugins.unavailable")
            return False
        selected = tuple(
            path for path, enabled in self._selected.items() if enabled
        )
        if not selected:
            return False

        from .asset_import_progress import AssetImportProgressService

        result: dict[str, object] = {}

        def work() -> bool:
            result["state"] = manager.install_package(
                self.package_path,
                selected=selected,
            )
            return True

        def complete(ok: bool, message: str) -> None:
            if not ok:
                self._message = message or t("inxpackage.import_failed")
                return
            state = result.get("state")
            reference = str(getattr(state, "reference", ""))
            self._message = t("inxpackage.imported").format(reference=reference)

        started = AssetImportProgressService.instance().begin(
            title=t("inxpackage.import_progress.title"),
            path=self.package_path,
            work=work,
            is_published=lambda: True,
            complete=complete,
            owner_id="inxpackage_import",
            preparing_message=t("inxpackage.import_progress.preparing"),
            processing_message=t("inxpackage.import_progress.processing"),
            publishing_message=t("inxpackage.import_progress.publishing"),
            complete_message=t("inxpackage.import_progress.complete"),
        )
        if not started:
            self._message = t("inxpackage.import_busy")
        return started

    def on_render_content(self, ctx) -> None:
        if self._preview is None:
            ctx.text_wrapped(t("inxpackage.no_package"))
            return
        metadata = self._preview.metadata
        summary_h = _metric(ctx, 112.0)
        footer_h = _metric(ctx, 46.0)
        gap = _metric(ctx, Theme.INSPECTOR_SECTION_GAP)
        start_y = ctx.get_cursor_pos_y()
        available_h = ctx.get_content_region_avail_height()
        content_y = start_y + summary_h + gap
        footer_y = start_y + max(0.0, available_h - footer_h)
        content_h = max(_metric(ctx, 48.0), footer_y - gap - content_y)

        ctx.push_style_color(ImGuiCol.ChildBg, *Theme.FRAME_BG)
        summary_visible = ctx.begin_child(
            "##inxpackage_summary", 0.0, summary_h, True, Theme.WINDOW_FLAGS_NO_SCROLL
        )
        if summary_visible:
            name = str(metadata.get("name") or metadata.get("reference") or "-")
            version = str(metadata.get("version", ""))
            ctx.label(name)
            if version:
                ctx.same_line(0.0, _metric(ctx, Theme.INSPECTOR_TITLE_GAP))
                ctx.push_style_color(ImGuiCol.Text, *Theme.TEXT_DIM)
                ctx.label(version)
                ctx.pop_style_color()
            intro = localized_intro(metadata, get_locale())
            if intro:
                ctx.text_wrapped(intro)
            ctx.push_style_color(ImGuiCol.Text, *Theme.TEXT_DIM)
            ctx.text_wrapped(self.package_path)
            ctx.pop_style_color()
            if self._message:
                color = Theme.ERROR_TEXT if ":" in self._message else Theme.SUCCESS_TEXT
                ctx.push_style_color(ImGuiCol.Text, *color)
                ctx.text_wrapped(self._message)
                ctx.pop_style_color()
        ctx.end_child()
        ctx.pop_style_color()

        ctx.set_cursor_pos_y(content_y)
        visible = ctx.begin_child("##inxpackage_entries", 0.0, content_h, True)
        if visible:
            selected_count = sum(self._selected.values())
            ctx.push_style_color(ImGuiCol.Text, *Theme.TEXT_DIM)
            ctx.label(
                t("inxpackage.entries_count").format(
                    selected=selected_count,
                    total=len(self._selected),
                )
            )
            ctx.pop_style_color()
            ctx.separator()
            for index, path in enumerate(self._preview.project_entries):
                self._selected[path] = bool(ctx.checkbox(f"{path}##inxpackage_{index}", self._selected.get(path, True)))
        ctx.end_child()

        ctx.set_cursor_pos_y(footer_y)
        ctx.push_style_color(ImGuiCol.ChildBg, *Theme.FRAME_BG)
        footer_visible = ctx.begin_child(
            "##inxpackage_footer", 0.0, footer_h, True, Theme.WINDOW_FLAGS_NO_SCROLL
        )
        if footer_visible:
            if ctx.button(
                t("inxpackage.select_all") + "##inxpackage_all",
                width=_metric(ctx, 96.0),
            ):
                self._selected = {path: True for path in self._selected}
            ctx.same_line()
            if ctx.button(
                t("inxpackage.select_none") + "##inxpackage_none",
                width=_metric(ctx, 96.0),
            ):
                self._selected = {path: False for path in self._selected}
            button_w = _metric(ctx, 104.0)
            action_x = ctx.get_cursor_pos_x() + max(0.0, ctx.get_content_region_avail_width() - button_w)
            ctx.same_line(action_x)
            ctx.begin_disabled(
                not any(self._selected.values()) or bool(self._install_block_reason)
            )
            if self._primary_button(ctx, t("inxpackage.import") + "##inxpackage_import"):
                self._begin_import()
            ctx.end_disabled()
        ctx.end_child()
        ctx.pop_style_color()

    @staticmethod
    def _primary_button(ctx, label: str) -> bool:
        ctx.push_style_color(ImGuiCol.Button, *Theme.APPLY_BUTTON)
        ctx.push_style_color(ImGuiCol.ButtonHovered, *Theme.PREFAB_BTN_HOVERED)
        ctx.push_style_color(ImGuiCol.ButtonActive, *Theme.PREFAB_BTN_ACTIVE)
        clicked = bool(ctx.button(label, width=_metric(ctx, 104.0)))
        ctx.pop_style_color(3)
        return clicked


__all__ = ["InxPackageImportPanel", "PluginPanel"]
