import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";

const docsRoot = path.resolve("docs");
const failures = [];

function fail(message) {
    failures.push(message);
}

async function exists(relative) {
    return stat(path.join(docsRoot, relative)).then(() => true).catch(() => false);
}

const rootPages = ["index.html", "start.html", "learn.html", "learn/shaders.html", "learn/renderstack-post-processing.html", "roadmap.html", "community.html", "download.html", "404.html"];
for (const page of rootPages) {
    const html = await readFile(path.join(docsRoot, page), "utf8");
    if (!html.includes("start.html")) fail(`${page}: missing the hand-maintained Start route`);
    if (/data-i18n=["']nav\.manual["']|>\s*(?:Manual|手册)\s*<\/a>/i.test(html)) fail(`${page}: obsolete Manual navigation is still present`);
    if (/wiki\/site\/(?:en|zh)\/(?:learn|manual|architecture)\//i.test(html)) fail(`${page}: links to a removed guide tree`);
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
for (const contract of ["data-learn-search", "data-learn-tag", "data-learn-entry", "learn/shaders.html", "learn/renderstack-post-processing.html", "js/learn.js?v=1"]) {
    if (!learn.includes(contract)) fail(`learn.html: missing '${contract}'`);
}
const shaderGuide = await readFile(path.join(docsRoot, "learn", "shaders.md"), "utf8");
if (!shaderGuide.includes("ShaderInfo") || !shaderGuide.includes("着色模型") || !shaderGuide.includes("Capabilities [Standalone]")) {
    fail("learn/shaders.md: shader learning contracts are incomplete");
}
const renderStackGuide = await readFile(path.join(docsRoot, "learn", "renderstack-post-processing.md"), "utf8");
if (!renderStackGuide.includes("RenderStack") || !renderStackGuide.includes("EffectStage") || !renderStackGuide.includes("后处理")) {
    fail("learn/renderstack-post-processing.md: RenderStack learning contracts are incomplete");
}

const download = await readFile(path.join(docsRoot, "download.html"), "utf8");
for (const contract of ["InfernuxHub", "<details class=\"advanced-download\">", "data-version-select", ".whl", "0.3.4", "0.2.9", "0.2.1", "js/download.js?v=5"]) {
    if (!download.includes(contract)) fail(`download.html: missing '${contract}'`);
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

console.log("Website verification passed: Start, rendering learning chapters, Hub-first downloads, API-only generated docs, and no Manual navigation.");
