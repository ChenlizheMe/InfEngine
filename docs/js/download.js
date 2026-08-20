(() => {
    "use strict";

    const releasesApiRoot = "https://api.github.com/repos/ChenlizheMe/Infernux/releases";
    const latestReleaseUrl = `${releasesApiRoot}/latest`;
    const publicReleasesUrl = `${releasesApiRoot}?per_page=100`;

    function releaseVersion(release) {
        return String(release?.tag_name || release?.version || "")
            .replace(/^v/i, "")
            .trim();
    }

    function releaseAssets(release) {
        return Array.isArray(release?.assets) ? release.assets : [];
    }

    function assetUrl(asset) {
        return asset?.browser_download_url || asset?.url || "";
    }

    function findAsset(release, pattern) {
        return releaseAssets(release).find((asset) => pattern.test(String(asset?.name || "")));
    }

    function findHub(release) {
        return findAsset(release, /^InfernuxHub(?:Installer)?[-_].*\.exe$/i);
    }

    function findWheel(release) {
        return findAsset(release, /^infernux-.*-cp312-cp312-win_amd64\.whl$/i);
    }

    function syncVersionLink(select) {
        const link = select.closest(".version-picker")?.querySelector("[data-version-link]");
        if (link) link.href = select.value;
    }

    function makeWheelOption(select, release, latestVersion) {
        const version = releaseVersion(release);
        const option = select.ownerDocument.createElement("option");
        option.value = assetUrl(findWheel(release));
        const isChinese = select.id.endsWith("-zh");
        option.textContent = version === latestVersion
            ? `${version} · ${isChinese ? "最新公开版本" : "latest public release"}`
            : version;
        return option;
    }

    function installReleaseWheels(select, releases, latestRelease) {
        const latestVersion = releaseVersion(latestRelease);
        const seen = new Set();
        const ordered = [latestRelease, ...releases].filter((release) => {
            if (release?.draft || release?.prerelease) return false;
            const version = releaseVersion(release);
            const wheelUrl = assetUrl(findWheel(release));
            if (!version || !wheelUrl || seen.has(version)) return false;
            seen.add(version);
            return true;
        });
        if (!ordered.length) return;

        const options = ordered.map((release) => makeWheelOption(select, release, latestVersion));
        select.replaceChildren(...options);
        select.value = options[0].value;
        syncVersionLink(select);
    }

    function applyReleaseCatalog({ latest, releases }) {
        const version = releaseVersion(latest);
        if (!version) throw new Error("latest release has no version tag");
        const hubUrl = assetUrl(findHub(latest));
        const wheelUrl = assetUrl(findWheel(latest));
        if (!hubUrl || !wheelUrl) throw new Error("latest release is missing the Hub or CPython wheel");

        document.querySelectorAll("[data-latest-hub]").forEach((link) => { link.href = hubUrl; });
        document.querySelectorAll("[data-hub-meta]").forEach((label) => {
            const isChinese = label.closest("[data-page-language='zh']") !== null;
            label.textContent = isChinese
                ? `Windows x64 · GitHub 最新公开版本 ${version}`
                : `Windows x64 · latest public release ${version}`;
        });
        document.querySelectorAll("[data-version-select]").forEach((select) => {
            installReleaseWheels(select, releases, latest);
        });
    }

    async function fetchReleaseJson(url) {
        const response = await fetch(url, {
            headers: { Accept: "application/vnd.github+json" },
            cache: "default"
        });
        if (!response.ok) throw new Error(`GitHub Releases returned ${response.status}`);
        return response.json();
    }

    async function loadReleaseCatalog() {
        const [latestResult, releasesResult] = await Promise.allSettled([
            fetchReleaseJson(latestReleaseUrl),
            fetchReleaseJson(publicReleasesUrl)
        ]);

        let latest = latestResult.status === "fulfilled" ? latestResult.value : null;
        const releases = releasesResult.status === "fulfilled" && Array.isArray(releasesResult.value)
            ? releasesResult.value
            : [];
        if (!latest) {
            latest = releases.find((release) => !release?.draft && !release?.prerelease) || null;
        }
        if (latest) return { latest, releases };

        try {
            const fallback = await fetch("release.json", { cache: "no-store" });
            if (!fallback.ok) throw new Error(`release fallback returned ${fallback.status}`);
            const release = await fallback.json();
            return { latest: release, releases: [release] };
        } catch (fallbackError) {
            throw latestResult.reason || releasesResult.reason || fallbackError;
        }
    }

    document.addEventListener("DOMContentLoaded", () => {
        document.querySelectorAll("[data-version-select]").forEach((select) => {
            syncVersionLink(select);
            select.addEventListener("change", () => syncVersionLink(select));
        });
        loadReleaseCatalog().then(applyReleaseCatalog).catch(() => {
            // The checked-in links remain a usable offline and rate-limit fallback.
        });
    });
})();
