(() => {
    "use strict";

    const catalogUrl = "hub-catalog.json";

    function syncVersionLink(select) {
        const link = select.closest(".version-picker")?.querySelector("[data-version-link]");
        if (link) link.href = select.value;
    }

    function stableRelease(catalog) {
        if (catalog?.$schema !== "infernux.hub_catalog" || !Array.isArray(catalog.releases)) {
            throw new Error("Hub catalog does not match the current contract");
        }
        const matches = catalog.releases.filter((release) => release?.version === catalog.stable);
        if (
            matches.length !== 1
            || matches[0].channel !== "stable"
            || typeof matches[0].minimum_updatable_version !== "string"
        ) {
            throw new Error("Hub catalog does not identify one stable release");
        }
        return matches[0];
    }

    function applyPlatform(release, platform) {
        const platformRelease = release.platforms?.[platform];
        const links = document.querySelectorAll(`[data-hub-link='${platform}']`);
        const labels = document.querySelectorAll(`[data-hub-meta='${platform}']`);
        const available = Boolean(platformRelease?.installer?.url);

        links.forEach((link) => {
            if (available) {
                link.href = platformRelease.installer.url;
                link.removeAttribute("aria-disabled");
                link.classList.remove("is-disabled");
            } else {
                link.removeAttribute("href");
                link.setAttribute("aria-disabled", "true");
                link.classList.add("is-disabled");
            }
        });
        labels.forEach((label) => {
            const isChinese = label.closest("[data-page-language='zh']") !== null;
            const platformLabel = platform === "windows-x64" ? "Windows x64" : "Linux x64";
            label.textContent = available
                ? `${platformLabel} · ${isChinese ? "最新公开版本" : "latest public release"} ${release.version}`
                : `${platformLabel} · ${isChinese ? "尚未公开发布" : "not publicly released yet"}`;
        });
    }

    async function loadHubCatalog() {
        const response = await fetch(catalogUrl, { cache: "no-store" });
        if (!response.ok) throw new Error(`Hub catalog returned ${response.status}`);
        return stableRelease(await response.json());
    }

    document.addEventListener("DOMContentLoaded", () => {
        document.querySelectorAll("[data-version-select]").forEach((select) => {
            syncVersionLink(select);
            select.addEventListener("change", () => syncVersionLink(select));
        });
        loadHubCatalog().then((release) => {
            applyPlatform(release, "windows-x64");
            applyPlatform(release, "linux-x64");
        });
    });
})();
