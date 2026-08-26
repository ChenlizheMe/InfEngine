import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";

const docsRoot = path.resolve("docs");
const failures = [];

function fail(message) {
    failures.push(message);
}

const release = JSON.parse(await readFile(path.join(docsRoot, "release.json"), "utf8"));
const releaseNotes = JSON.parse(await readFile(path.join(docsRoot, "release-notes.json"), "utf8"));
const docsManifest = JSON.parse(await readFile(path.join(docsRoot, "docs-manifest.json"), "utf8"));
const apiIndex = JSON.parse(await readFile(path.join(docsRoot, "api-index.json"), "utf8"));
const apiChanges = JSON.parse(await readFile(path.join(docsRoot, "api-changes.json"), "utf8"));
const pyproject = await readFile(path.resolve("pyproject.toml"), "utf8");
const packageVersion = pyproject.match(/^version\s*=\s*"([^"]+)"/m)?.[1] || "";
const currentVersion = String(release.version || "").trim();

function parseVersion(value) {
    const match = /^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$/.exec(String(value || "").trim());
    return match ? match.slice(1).map(Number) : null;
}

function compareVersions(left, right) {
    for (let index = 0; index < 3; index += 1) {
        if (left[index] !== right[index]) return left[index] - right[index];
    }
    return 0;
}

if (!currentVersion) fail("release.json: missing the current release version");
if (release.tag !== `v${currentVersion}`) fail(`release.json: tag '${release.tag}' does not match version ${currentVersion}`);
for (const asset of release.assets || []) {
    if (!String(asset.name || "").includes(currentVersion) || !String(asset.url || "").includes(`/v${currentVersion}/`)) {
        fail(`release.json: ${asset.kind || "asset"} does not target current release ${currentVersion}`);
    }
}
if (docsManifest.documented_release !== currentVersion) {
    fail(`docs-manifest.json: documented_release ${docsManifest.documented_release} does not match release.json ${currentVersion}`);
}
const packageVersionParts = parseVersion(packageVersion);
const currentVersionParts = parseVersion(currentVersion);
if (!packageVersionParts) {
    fail(`pyproject.toml: package version ${packageVersion || "<missing>"} is invalid`);
} else if (!currentVersionParts) {
    fail(`release.json: release version ${currentVersion || "<missing>"} is invalid`);
} else if (compareVersions(packageVersionParts, currentVersionParts) < 0) {
    fail(`pyproject.toml: package version ${packageVersion} is older than published release ${currentVersion}`);
}
if (releaseNotes.version !== currentVersion || releaseNotes.tag !== `v${currentVersion}`) {
    fail(`release-notes.json: version/tag does not match current release ${currentVersion}`);
}
if (apiIndex.generated_for_release !== currentVersion) {
    fail(`api-index.json: generated_for_release ${apiIndex.generated_for_release} does not match current release ${currentVersion}`);
}
if (apiChanges.current_release !== currentVersion) {
    fail(`api-changes.json: current_release ${apiChanges.current_release} does not match current release ${currentVersion}`);
}
const currentSnapshotPath = path.join(docsRoot, "api-snapshots", `${currentVersion}.json`);
const currentSnapshot = await readFile(currentSnapshotPath, "utf8")
    .then((content) => JSON.parse(content))
    .catch(() => null);
if (!currentSnapshot) fail(`api-snapshots/${currentVersion}.json: current release snapshot is missing or invalid`);
else if (currentSnapshot.release !== currentVersion) {
    fail(`api-snapshots/${currentVersion}.json: snapshot release ${currentSnapshot.release} does not match ${currentVersion}`);
}

const releaseVersionContracts = [
    ["README.md", `version-${currentVersion}-orange.svg`],
    ["README.md", `## ${currentVersion}`],
    ["README.md", `version = {${currentVersion}}`],
    ["README-zh.md", `version-${currentVersion}-orange.svg`],
    ["README-zh.md", `## ${currentVersion}`],
    ["README-zh.md", `version = {${currentVersion}}`],
];
const packageVersionContracts = [
    ["packaging/windows_version_info.txt", `'${packageVersion}.0'`],
    ["packaging/windows_version_info.txt", `filevers=(${packageVersion.replaceAll(".", ", ")}, 0)`],
    ["packaging/windows_version_info.txt", `prodvers=(${packageVersion.replaceAll(".", ", ")}, 0)`],
    ["cpp/infernux/tools/launcher/InfernuxPlayerLauncher.rc", `"${packageVersion}.0"`],
    ["cpp/infernux/tools/launcher/InfernuxPlayerLauncher.rc", `FILEVERSION ${packageVersion.replaceAll(".", ",")},0`],
    ["cpp/infernux/tools/launcher/InfernuxPlayerLauncher.rc", `PRODUCTVERSION ${packageVersion.replaceAll(".", ",")},0`],
];
for (const [relative, token] of [...releaseVersionContracts, ...packageVersionContracts]) {
    const content = await readFile(path.resolve(relative), "utf8");
    if (!content.includes(token)) fail(`${relative}: missing version contract '${token}'`);
}

async function exists(relative) {
    return stat(path.join(docsRoot, relative)).then(() => true).catch(() => false);
}

const learningCourses = JSON.parse(
    await readFile(path.join(docsRoot, "learn", "learning-courses.json"), "utf8")
);
const learningChapters = (await Promise.all(learningCourses.map(async (course) => JSON.parse(
    await readFile(path.join(docsRoot, "learn", course.manifest), "utf8")
)))).flat();
const rootPages = [
    "index.html",
    "start.html",
    "learn.html",
    ...learningCourses.map((course) => `learn/${course.slug}.html`),
    ...learningChapters.map((chapter) => `learn/${chapter.slug}.html`),
    "roadmap.html",
    "community.html",
    "download.html",
    "404.html",
];
for (const page of rootPages) {
    const html = await readFile(path.join(docsRoot, page), "utf8");
    if (!html.includes("start.html")) fail(`${page}: missing the hand-maintained Start route`);
    if (/data-i18n=["']nav\.manual["']|>\s*(?:Manual|手册)\s*<\/a>/i.test(html)) fail(`${page}: obsolete Manual navigation is still present`);
    if (/wiki\/site\/(?:en|zh)\/(?:learn|manual|architecture)\//i.test(html)) fail(`${page}: links to a removed guide tree`);
    const ribbon = html.match(/<span class="mission-kicker" data-i18n="brand\.ribbonKicker">([^<]+)<\/span>/);
    if (ribbon && !ribbon[1].includes(currentVersion)) {
        fail(`${page}: identity ribbon fallback '${ribbon[1]}' does not match current release ${currentVersion}`);
    }
}

const homepage = await readFile(path.join(docsRoot, "index.html"), "utf8");
if (!homepage.includes(`"softwareVersion": "${currentVersion}"`)) {
    fail(`index.html: structured softwareVersion does not match current release ${currentVersion}`);
}
if (!homepage.includes(`>v${currentVersion}</div>`)) {
    fail(`index.html: current status card does not show v${currentVersion}`);
}

const roadmap = await readFile(path.join(docsRoot, "roadmap.html"), "utf8");
if (!roadmap.includes(`<strong>v${currentVersion}</strong>`)) {
    fail(`roadmap.html: current release card does not show v${currentVersion}`);
}

const i18nSource = JSON.parse(await readFile(path.join(docsRoot, "tools", "i18n-source.json"), "utf8"));
for (const language of ["en", "zh"]) {
    for (const key of ["brand.ribbonKicker", "home.hero.badge", "home.hero.platform", "home.capabilities.kicker", "roadmap.hero.badge"]) {
        if (!String(i18nSource[language]?.[key] || "").includes(currentVersion)) {
            fail(`i18n-source.json: ${language}.${key} does not contain current release ${currentVersion}`);
        }
    }
}

for (const language of ["en", "zh"]) {
    const apiIndex = await readFile(path.join(docsRoot, "wiki", "docs", language, "api", "index.md"), "utf8");
    if (!apiIndex.includes(currentVersion)) {
        fail(`wiki/docs/${language}/api/index.md: API landing page does not show current release ${currentVersion}`);
    }
}

const start = await readFile(path.join(docsRoot, "start.html"), "utf8");
for (const contract of [
    "Edit this file directly",
    'data-page-language="en"',
    'data-page-language="zh"',
    'id="first-script"',
    "js/bilingual-page.js",
]) {
    if (!start.includes(contract)) fail(`start.html: missing '${contract}'`);
}
if (/since|last_verified|始于|验证于|zh\/manual\//i.test(start)) fail("start.html: generated-document metadata or source paths leaked into the simple guide");

const learn = await readFile(path.join(docsRoot, "learn.html"), "utf8");
for (const contract of ["learn/gameplay.html", "learn/rendering.html", "learn-course-grid", "Build gameplay with Python", "编写自定义渲染"]) {
    if (!learn.includes(contract)) fail(`learn.html: missing '${contract}'`);
}
for (const course of learningCourses) {
    const coursePage = await readFile(path.join(docsRoot, "learn", `${course.slug}.html`), "utf8");
    for (const contract of ["data-learn-search", "data-learn-tag", "data-learn-entry", course.title_en, course.title_zh, "js/learn.js?v=3"]) {
        if (!coursePage.includes(contract)) fail(`learn/${course.slug}.html: missing '${contract}'`);
    }
}
for (const chapter of learningChapters) {
    const markdownPath = path.join(docsRoot, "learn", `${chapter.slug}.md`);
    const htmlPath = path.join(docsRoot, "learn", `${chapter.slug}.html`);
    const markdown = await readFile(markdownPath, "utf8");
    const rendered = await readFile(htmlPath, "utf8");
    if (!markdown.includes("<!-- language:zh -->") || !markdown.includes("<figure") || !markdown.includes("../assets/learn/")) {
        fail(`learn/${chapter.slug}.md: bilingual content or reviewed figures are missing`);
    }
    if (markdown.includes("learn-figure-placeholder")) {
        fail(`learn/${chapter.slug}.md: obsolete figure placeholder remains`);
    }
    if (/\.\.?\/[^)"'\s]+\.(?:png|jpe?g)(?:[?#][^)"'\s]*)?/i.test(markdown)) {
        fail(`learn/${chapter.slug}.md: tutorial figures must use WebP only`);
    }
    for (const token of [chapter.title_en, chapter.title_zh, `${chapter.slug}.md`, "data-page-language=\"en\"", "data-page-language=\"zh\""]) {
        if (!rendered.includes(token)) fail(`learn/${chapter.slug}.html: missing '${token}'`);
    }
}

for (const asset of await readdir(path.join(docsRoot, "assets", "learn"))) {
    if (/\.(?:png|jpe?g)$/i.test(asset)) {
        fail(`assets/learn/${asset}: tutorial raster assets must use WebP only`);
    }
}

const download = await readFile(path.join(docsRoot, "download.html"), "utf8");
for (const contract of ["InfernuxHub", "<details class=\"advanced-download\">", "data-version-select", ".whl", currentVersion, "0.3.4", "0.2.9", "0.2.1", "js/download.js?v=5"]) {
    if (!download.includes(contract)) fail(`download.html: missing '${contract}'`);
}
if (!download.includes(`/download/v${currentVersion}/InfernuxHubInstaller-${currentVersion}.exe`)) {
    fail(`download.html: offline Hub fallback does not target v${currentVersion}`);
}
if (!download.includes(`/download/v${currentVersion}/infernux-${currentVersion}-cp312-cp312-win_amd64.whl`)) {
    fail(`download.html: offline wheel fallback does not target v${currentVersion}`);
}
if (/SHA-?256|checksum|校验码|publisher signature|data-pwa-install|pwa-install\.js/i.test(download)) {
    fail("download.html: verification or documentation-app installation clutter was restored");
}
if (/<details class="advanced-download"\s+open/i.test(download)) fail("download.html: advanced WHL downloads must remain collapsed by default");

for (const language of ["en", "zh"]) {
    for (const section of ["learn", "manual", "architecture"]) {
        if (await exists(path.join("wiki", "docs", language, section))) fail(`wiki/docs/${language}/${section}: removed guide source still exists`);
        if (await exists(path.join("wiki", "site", language, section))) fail(`wiki/site/${language}/${section}: removed generated guide still exists`);
    }
}

for (const obsolete of [
    "docs-index.json",
    "docs-health.json",
    "learning-paths.json",
    "llms.txt",
    "llms-full.txt",
    path.join("js", "wiki.js"),
    path.join("js", "docs-health.js"),
    path.join("js", "pwa-install.js"),
]) {
    if (await exists(obsolete)) fail(`${obsolete}: obsolete generated-guide artifact still exists`);
}

const mkdocs = await readFile(path.join(docsRoot, "wiki", "mkdocs.yml"), "utf8");
if (/(?:en|zh)\/(?:learn|manual|architecture)\//.test(mkdocs)) fail("mkdocs.yml: removed guide navigation was restored");
for (const apiRoute of ["en/api/index.md", "zh/api/index.md"]) {
    if (!mkdocs.includes(apiRoute)) fail(`mkdocs.yml: missing ${apiRoute}`);
}

for (const language of ["en", "zh"]) {
    const apiRoot = path.join(docsRoot, "wiki", "docs", language, "api");
    const files = (await readdir(apiRoot)).filter((name) => name.endsWith(".md"));
    if (!files.length) fail(`${language} API source is empty`);
    for (const file of files) {
        const markdown = await readFile(path.join(apiRoot, file), "utf8");
        if (/\.\.\/(?:learn|manual|architecture)\//.test(markdown)) fail(`${language}/api/${file}: links to a removed guide`);
    }
}

if (failures.length) {
    console.error(`Website verification failed with ${failures.length} issue(s):`);
    for (const failure of failures) console.error(`- ${failure}`);
    process.exit(1);
}

console.log("Website verification passed: Start, multi-course Learn, Hub-first downloads, API-only generated docs, and no Manual navigation.");
