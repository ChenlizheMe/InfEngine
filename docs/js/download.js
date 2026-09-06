(() => {
    "use strict";

    const catalogUrl = "hub-catalog.json";
    let currentRelease = null;

    function syncVersionLink(select) {
        const link = select.closest(".version-picker")?.querySelector("[data-version-link]");
        if (!link || !currentRelease) return;
        const pending = currentRelease && !currentRelease.published_at
            && select.value.includes(`/v${currentRelease.version}/`);
        if (pending) {
            link.removeAttribute("href");
            link.setAttribute("aria-disabled", "true");
            link.classList.add("is-disabled");
        } else {
            link.href = select.value;
            link.removeAttribute("aria-disabled");
            link.classList.remove("is-disabled");
        }
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
        const available = Boolean(release.published_at && platformRelease?.installer?.url);

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
                ? `${platformLabel} · ${release.version}`
                : `${platformLabel} · ${release.version} · ${isChinese ? "制品待发布" : "release files pending publication"}`;
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
            currentRelease = release;
            applyPlatform(release, "windows-x64");
            applyPlatform(release, "linux-x64");
            document.querySelectorAll("[data-version-select]").forEach(syncVersionLink);
        });
    });
})();
