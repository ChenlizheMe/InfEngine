"""
Preferences — floating editor preferences window.
"""

from __future__ import annotations

from Infernux.engine.i18n import t, get_locale
from Infernux.engine.ide_preference import get_ide
from Infernux.engine.interaction import (
    CommandSource,
    EditorInteractionCore,
    KeyChord,
    PanelInteractionDescriptor,
    submit_preferences_command,
)
from .editor_panel import FloatingEditorPanel
from .panel_registry import editor_panel
from .project_utils import detect_available_ides


_LOCALES = ["en", "zh"]
_LOCALE_LABELS = ["English", "简体中文"]


@editor_panel(
    "Preferences",
    type_id="preferences",
    title_key="prefs.title",
    menu_path="",
    interaction=PanelInteractionDescriptor(),
)
class PreferencesPanel(FloatingEditorPanel):
    """User Preferences utility surface hosted by the global panel lifecycle."""

    def __init__(self) -> None:
        super().__init__(
            title="Preferences",
            window_id="preferences",
            size=(980.0, 720.0),
        )
        self._shortcut_search = ""
        self._shortcut_new_profile_name = ""
        self._shortcut_profile_name_buffers: dict[str, str] = {}
        self._shortcut_binding_buffers: dict[str, str] = {}
        self._shortcut_error = ""
        self._shortcut_profile_revision = -1

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def on_render_content(self, ctx) -> None:
        self._render_body(ctx)

    def _render_body(self, ctx) -> None:
        ctx.label(t("prefs.language"))
        ctx.same_line(150)
        avail = ctx.get_content_region_avail_width()
        ctx.set_next_item_width(avail)

        current_idx = _LOCALES.index(get_locale()) if get_locale() in _LOCALES else 0
        new_idx = ctx.combo("##language", current_idx, _LOCALE_LABELS)
        if new_idx != current_idx:
            submit_preferences_command(
                "preferences.set_locale",
                source=CommandSource.POINTER,
                value=_LOCALES[new_idx],
            )

        ctx.label(t("prefs.ide"))
        ctx.same_line(150)
        avail = ctx.get_content_region_avail_width()
        ctx.set_next_item_width(avail)

        available_ides = detect_available_ides()
        current_ide = get_ide()

        if available_ides:
            if current_ide not in available_ides:
                current_ide = available_ides[0]

            ide_labels = []
            for ide in available_ides:
                if ide == "vscode":
                    ide_labels.append(t("prefs.ide.vscode"))
                elif ide == "pycharm":
                    ide_labels.append(t("prefs.ide.pycharm"))
                else:
                    ide_labels.append(ide)

            current_ide_idx = available_ides.index(current_ide)
            new_ide_idx = ctx.combo("##preferred_ide", current_ide_idx, ide_labels)
            if new_ide_idx != current_ide_idx:
                submit_preferences_command(
                    "preferences.set_ide",
                    source=CommandSource.POINTER,
                    value=available_ides[new_ide_idx],
                )

            ctx.text_wrapped(t("prefs.ide.available_hint"))
        else:
            ctx.text_wrapped(t("prefs.ide.none_available"))

        ctx.separator()
        self._render_shortcuts(ctx)

    def _render_shortcuts(self, ctx) -> None:
        core = EditorInteractionCore.instance()
        if core is None:
            return
        model = core.preferences.shortcut_profiles
        if model is None:
            ctx.text_wrapped(t("prefs.shortcuts.unavailable"))
            return
        if self._shortcut_profile_revision != model.revision:
            self._shortcut_profile_revision = model.revision
            self._shortcut_binding_buffers.clear()

        snapshot = model.snapshot
        profiles = snapshot.profiles
        active_index = next(
            index
            for index, profile in enumerate(profiles)
            if profile.profile_id == snapshot.active_profile_id
        )

        ctx.label(t("prefs.shortcuts"))
        ctx.label(t("prefs.shortcuts.profile"))
        ctx.same_line(150)
        ctx.set_next_item_width(260.0)
        selected_index = ctx.combo(
            "##shortcut_profile",
            active_index,
            [profile.name for profile in profiles],
        )
        if selected_index != active_index:
            self._execute_shortcut_command(
                "preferences.shortcut.activate_profile",
                {"profile_id": profiles[selected_index].profile_id},
            )
            snapshot = model.snapshot

        active = snapshot.profile(snapshot.active_profile_id)
        if active.is_default:
            ctx.text_wrapped(t("prefs.shortcuts.default_read_only"))
            self._render_shortcut_profile_creation(ctx)
        else:
            name_buffer = self._shortcut_profile_name_buffers.setdefault(
                active.profile_id,
                active.name,
            )
            ctx.label(t("prefs.shortcuts.profile_name"))
            ctx.same_line(150)
            ctx.set_next_item_width(260.0)
            name_buffer = ctx.text_input(
                "##shortcut_profile_name",
                name_buffer,
                128,
            )
            self._shortcut_profile_name_buffers[active.profile_id] = name_buffer
            ctx.same_line()
            if ctx.button(t("prefs.shortcuts.rename") + "##shortcut_profile_rename"):
                if self._execute_shortcut_command(
                    "preferences.shortcut.rename_profile",
                    {"profile_id": active.profile_id, "name": name_buffer},
                ):
                    self._shortcut_profile_name_buffers[active.profile_id] = (
                        model.profile(active.profile_id).name
                    )
            ctx.same_line()
            if ctx.button(t("prefs.shortcuts.delete") + "##shortcut_profile_delete"):
                self._execute_shortcut_command(
                    "preferences.shortcut.delete_profile",
                    {"profile_id": active.profile_id},
                )
            ctx.same_line()
            if ctx.button(t("prefs.shortcuts.reset_all") + "##shortcut_profile_reset"):
                self._execute_shortcut_command(
                    "preferences.shortcut.reset_profile",
                    {},
                )
            self._render_shortcut_profile_creation(ctx)

        ctx.label(t("prefs.shortcuts.search"))
        ctx.same_line(150)
        ctx.set_next_item_width(-1.0)
        self._shortcut_search = ctx.text_input(
            "##shortcut_search",
            self._shortcut_search,
            256,
        )
        if self._shortcut_error:
            ctx.text_wrapped(self._shortcut_error)

        available_height = max(180.0, ctx.get_content_region_avail_height())
        if ctx.begin_child("##shortcut_bindings", 0.0, available_height, True):
            self._render_shortcut_bindings(ctx, core, model)
        ctx.end_child()

    def _render_shortcut_profile_creation(self, ctx) -> None:
        ctx.label(t("prefs.shortcuts.new_profile"))
        ctx.same_line(150)
        ctx.set_next_item_width(260.0)
        self._shortcut_new_profile_name = ctx.text_input(
            "##shortcut_new_profile_name",
            self._shortcut_new_profile_name,
            128,
        )
        ctx.same_line()
        if ctx.button(t("prefs.shortcuts.create") + "##shortcut_profile_create"):
            if self._execute_shortcut_command(
                "preferences.shortcut.create_profile",
                {"name": self._shortcut_new_profile_name},
            ):
                self._shortcut_new_profile_name = ""

    def _render_shortcut_bindings(self, ctx, core, model) -> None:
        query = self._shortcut_search.strip().casefold()
        active_is_default = model.profile(model.active_profile_id).is_default
        for binding in model.snapshot.bindings:
            command = core.commands.get(binding.command_id)
            display_name = (
                command.display_name
                if command is not None and command.display_name
                else binding.command_id
            )
            category = command.category if command is not None else ""
            searchable = " ".join(
                (display_name, category, binding.command_id, binding.binding_id)
            ).casefold()
            if query and query not in searchable:
                continue

            ctx.label(display_name)
            if category:
                ctx.same_line(290.0)
                ctx.label(category)
            current_chord = (
                ""
                if binding.effective_chord is None
                else binding.effective_chord.display_name()
            )
            buffer = self._shortcut_binding_buffers.setdefault(
                binding.binding_id,
                current_chord,
            )
            ctx.set_next_item_width(220.0)
            buffer = ctx.text_input(
                f"##shortcut_binding_{binding.binding_id}",
                buffer,
                64,
            )
            self._shortcut_binding_buffers[binding.binding_id] = buffer

            if not active_is_default:
                ctx.same_line()
                if ctx.button(
                    t("prefs.shortcuts.apply")
                    + f"##shortcut_apply_{binding.binding_id}"
                ):
                    self._apply_shortcut_buffer(model, binding.binding_id, buffer)
                ctx.same_line()
                if ctx.button(
                    t("prefs.shortcuts.disable")
                    + f"##shortcut_disable_{binding.binding_id}"
                ):
                    if self._execute_shortcut_command(
                        "preferences.shortcut.assign",
                        {"binding_id": binding.binding_id, "disabled": True},
                    ):
                        self._shortcut_binding_buffers[binding.binding_id] = ""
                if binding.overridden:
                    ctx.same_line()
                    if ctx.button(
                        t("prefs.shortcuts.reset")
                        + f"##shortcut_reset_{binding.binding_id}"
                    ):
                        if self._execute_shortcut_command(
                            "preferences.shortcut.reset_binding",
                            {"binding_id": binding.binding_id},
                        ):
                            restored = model.binding(binding.binding_id)
                            self._shortcut_binding_buffers[binding.binding_id] = (
                                restored.effective_chord.display_name()
                                if restored.effective_chord is not None
                                else ""
                            )
            ctx.separator()

    def _apply_shortcut_buffer(self, model, binding_id: str, value: str) -> bool:
        try:
            chord = KeyChord.parse(value).display_name()
        except (TypeError, ValueError) as exc:
            self._shortcut_error = str(exc)
            return False
        conflicts = model.conflicts_for(binding_id, chord)
        if conflicts:
            self._shortcut_error = t("prefs.shortcuts.conflict").format(
                bindings=", ".join(conflict.binding_id for conflict in conflicts)
            )
            return False
        return self._execute_shortcut_command(
            "preferences.shortcut.assign",
            {"binding_id": binding_id, "chord": chord},
        )

    def _execute_shortcut_command(self, command_id: str, payload: dict) -> bool:
        core = EditorInteractionCore.instance()
        if core is None:
            self._shortcut_error = t("prefs.shortcuts.unavailable")
            return False
        result = core.commands.execute(
            command_id,
            source=CommandSource.POINTER,
            payload=payload,
        )
        if not result.accepted:
            self._shortcut_error = result.message or t("prefs.shortcuts.rejected")
            return False
        self._shortcut_error = ""
        return True
