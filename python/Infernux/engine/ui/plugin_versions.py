"""Explicit version selection and local-edit consent for installed packages."""

from __future__ import annotations

from Infernux.engine.i18n import t
from Infernux.plugins import PackageUpdateConflict, plugin_install_block_reason

from .plugin_install_progress import PluginInstallProgressService


class PluginVersionsView:
    def __init__(self) -> None:
        self.releases: dict[str, tuple[dict[str, object], ...]] = {}
        self.selection: dict[str, int] = {}
        self.messages: dict[str, str] = {}
        self.pending: dict[str, dict[str, object]] = {}

    def render(self, ctx, manager, row) -> None:
        reference = str(row["reference"])
        current = str(row.get("version", ""))
        ctx.text_wrapped(t("plugins.versions.installed").format(version=current))
        if not manager.release_repository(reference):
            ctx.text_wrapped(t("plugins.versions.local"))
            return
        if ctx.button(t("plugins.versions.check") + "##plugin_check_versions"):
            self.check(manager, reference)
        versions = self.releases.get(reference, ())
        if versions:
            index = ctx.combo("##plugin_release_version", self.selection.get(reference, 0),
                              [str(item["version"]) for item in versions])
            if index != self.selection.get(reference, 0):
                self.pending.pop(reference, None)
                self.messages.pop(reference, None)
            self.selection[reference] = index
            selected = versions[index]
            ctx.text_wrapped(str(selected.get("notes", "")) or t("plugins.versions.no_notes"))
            block = plugin_install_block_reason(reference)
            disabled = str(selected["version"]) == current or bool(block)
            ctx.begin_disabled(disabled)
            if ctx.button(t("plugins.versions.apply") + "##plugin_update_version"):
                self.download(manager, reference, str(selected["release_tag"]))
            ctx.end_disabled()
            if block:
                ctx.text_wrapped(t(block))
        elif reference in self.releases:
            ctx.text_wrapped(t("plugins.versions.none"))
        if reference in self.pending:
            ctx.separator()
            ctx.text_wrapped(t("plugins.versions.conflict"))
            if ctx.button(t("plugins.versions.overwrite") + "##plugin_update_overwrite"):
                self.publish(manager, reference, overwrite=True)
            ctx.same_line()
            if ctx.button(t("plugins.versions.cancel") + "##plugin_update_cancel"):
                self.pending.pop(reference, None)
                self.messages.pop(reference, None)
        if self.messages.get(reference):
            ctx.text_wrapped(self.messages[reference])

    def check(self, manager, reference: str) -> None:
        self.pending.pop(reference, None)
        def complete(ok, result, message):
            if ok:
                self.releases[reference] = tuple(result)
                self.selection[reference] = 0
                self.messages.pop(reference, None)
            else:
                self.messages[reference] = message

        self._begin(reference, lambda report: manager.available_releases(reference), complete)

    def download(self, manager, reference: str, tag: str) -> None:
        self.pending.pop(reference, None)

        def complete(ok, result, message):
            if not ok:
                self.messages[reference] = message
                return
            self.pending[reference] = result
            # Progress completion runs on the editor thread. Lifecycle publication
            # must not happen in the network worker or while an old preload runs.
            self.publish(manager, reference)

        self._begin(reference, lambda report: manager.download_update(reference, tag, progress=report), complete)

    def publish(self, manager, reference: str, *, overwrite: bool = False) -> None:
        release = self.pending[reference]
        try:
            manager.install_package(str(release["path"]), source=release["source"],
                                    update=True, overwrite_modified=overwrite)
        except PackageUpdateConflict as exc:
            self.messages[reference] = "\n".join(exc.paths)
        except Exception as exc:
            self.pending.pop(reference, None)
            self.messages[reference] = f"{type(exc).__name__}: {exc}"
        else:
            self.pending.pop(reference, None)
            self.messages[reference] = t("plugins.versions.updated")

    def _begin(self, reference, work, complete) -> None:
        if not PluginInstallProgressService.instance().begin(
            label=reference, work=work, complete=complete,
        ):
            self.messages[reference] = t("plugins.install_progress.busy")
