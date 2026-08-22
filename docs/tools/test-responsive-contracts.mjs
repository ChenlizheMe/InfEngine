import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const docsRoot = path.resolve(scriptDir, "..");

async function read(relativePath) {
    return readFile(path.join(docsRoot, ...relativePath.split("/")), "utf8");
}

function maxWidthBreakpoints(css) {
    return [...css.matchAll(/@media\s*\(\s*max-width:\s*(\d+)px\s*\)/g)]
        .map((match) => Number(match[1]));
}

function activeBreakpoints(css, viewportWidth) {
    return [...new Set(maxWidthBreakpoints(css).filter((breakpoint) => viewportWidth <= breakpoint))]
        .sort((a, b) => a - b);
}

function assertBreakpoint(css, breakpoint, surface) {
    assert.ok(maxWidthBreakpoints(css).includes(breakpoint), `${surface} must declare its ${breakpoint}px responsive branch`);
}

const rootPages = [
    ["index.html", "Home"],
    ["start.html", "Start"],
    ["learn.html", "Learn"],
    ["learn/gameplay.html", "Gameplay course"],
    ["learn/rendering.html", "Rendering course"],
    ["learn/gameplay-first-component.html", "Gameplay chapter"],
    ["learn/rendering-overview.html", "Rendering chapter"],
    ["roadmap.html", "Roadmap"],
    ["community.html", "Community"],
    ["download.html", "Download"],
    ["404.html", "Not-found recovery"],
    ["offline.html", "Offline recovery"]
];
const viewportContract = /<meta\s+name=["']viewport["']\s+content=["']width=device-width, initial-scale=1\.0["']\s*\/?>/i;

for (const [relativePath, surface] of rootPages) {
    const html = await read(relativePath);
    assert.match(html, viewportContract, `${surface} must opt into the device viewport`);
    assert.doesNotMatch(html, /\sstyle\s*=/i, `${surface} must not reintroduce fixed inline layout styles`);
}

for (const relativePath of ["index.html", "start.html", "learn.html", "learn/gameplay.html", "learn/rendering.html", "learn/gameplay-first-component.html", "learn/rendering-overview.html", "roadmap.html", "community.html", "download.html", "404.html"]) {
    const html = await read(relativePath);
    assert.match(html, /<a[^>]+class=["'][^"']*skip-link[^"']*["'][^>]+href=["']#main-content["']/i, `${relativePath} must retain a skip link`);
    assert.match(html, /<main\b[^>]*\bid=["']main-content["']/i, `${relativePath} must retain one addressable main region`);
}

const styles = {
    shared: await read("css/style.css"),
    home: await read("css/home.css"),
    start: await read("css/start.css"),
    learn: await read("css/learn.css"),
    download: await read("css/download.css"),
    roadmap: await read("css/roadmap.css"),
    search: await read("css/docs-search.css"),
    generated: await read("css/wiki-generated.css")
};
const template = await read("wiki/theme/main.html");
const hashedTemplateStyles = (await readdir(path.join(docsRoot, "css")))
    .filter((name) => /^wiki-template\.[a-f0-9]{16}\.css$/.test(name));
assert.equal(hashedTemplateStyles.length, 1, "generated documentation must publish exactly one content-hashed template stylesheet");
const templateStyles = await read(`css/${hashedTemplateStyles[0]}`);

for (const breakpoint of [1180, 1080, 820, 520]) assertBreakpoint(styles.shared, breakpoint, "Shared shell");
assertBreakpoint(styles.start, 700, "Start");
assertBreakpoint(styles.learn, 720, "Learn");
assertBreakpoint(styles.download, 700, "Download");
for (const breakpoint of [1080, 820]) assertBreakpoint(styles.roadmap, breakpoint, "Roadmap");
assertBreakpoint(styles.search, 720, "Documentation search");
assertBreakpoint(styles.home, 520, "Home starter action");
assertBreakpoint(styles.generated, 768, "Generated-document enhancements");
for (const css of [template, templateStyles]) {
    assertBreakpoint(css, 1024, "Generated-document template");
    assertBreakpoint(css, 768, "Generated-document template");
}

const surfaces = {
    Home: `${styles.shared}\n${styles.home}`,
    "Not-found recovery": `${styles.shared}\n${styles.search}`,
    Start: `${styles.shared}\n${styles.start}\n${styles.search}`,
    Learn: `${styles.shared}\n${styles.learn}\n${styles.search}`,
    Roadmap: `${styles.shared}\n${styles.roadmap}\n${styles.search}`,
    Download: `${styles.shared}\n${styles.download}\n${styles.search}`,
    "Generated documentation": `${styles.shared}\n${styles.generated}\n${styles.search}\n${templateStyles}`
};
for (const [surface, css] of Object.entries(surfaces)) {
    assert.ok(activeBreakpoints(css, 375).length > 0, `${surface} must have a 375px narrow-screen branch`);
    assert.ok(activeBreakpoints(css, 768).length > 0, `${surface} must have a 768px tablet branch`);
    assert.deepEqual(activeBreakpoints(css, 1440), [], `${surface} must retain an unmodified 1440px desktop base`);
}

for (const contract of [
    "-webkit-text-size-adjust: 100%",
    "text-size-adjust: 100%",
    "img { max-width: 100%",
    "overflow-wrap: anywhere",
    "table { max-width: 100%",
    "@media (prefers-reduced-motion: reduce)",
    "@media (prefers-contrast: more)",
    "@media (forced-colors: active)"
]) {
    assert.ok(styles.shared.includes(contract), `shared styles are missing text/zoom resilience contract '${contract}'`);
}
assert.match(styles.shared, /\.nav-links\.mobile-open a\s*\{[\s\S]*?min-height:\s*44px;/, "mobile navigation links must remain at least 44px high");
assert.match(styles.shared, /@media\s*\(max-width:\s*520px\)[\s\S]*?\.btn\s*\{[^}]*width:\s*100%;/, "375px calls to action must expand to a single-thumb target");
assert.match(styles.search, /@media\s*\(max-width:\s*720px\)[\s\S]*?\.docs-search-trigger\s*\{[^}]*width:\s*44px;[^}]*height:\s*44px;/, "mobile search must retain a 44px trigger");
assert.match(styles.home, /\.code-copy-action\s*\{[\s\S]*?min-height:\s*44px;/, "home starter copy must retain a 44px minimum touch target");
assert.match(styles.home, /@media\s*\(max-width:\s*520px\)[\s\S]*?\.code-copy-action\s*\{[^}]*width:\s*44px;[^}]*min-width:\s*44px;[\s\S]*?\.code-copy-action span\s*\{[^}]*clip:/, "phone starter copy must collapse its visual label without removing the accessible name");
assert.match(styles.generated, /@media\s*\(max-width:\s*768px\)[\s\S]*?\.doc-outline-link\s*\{\s*min-height:\s*44px;/, "mobile document outlines must retain 44px links");
assert.match(templateStyles, /\.api-main\s*\{[^}]*min-width:\s*0;/, "generated document content must be allowed to shrink inside its layout");
assert.match(templateStyles, /@media\s*\(max-width:\s*768px\)[\s\S]*?\.api-main td,[\s\S]*?overflow-wrap:\s*anywhere;/, "API tables must wrap long symbols on narrow screens");
assert.match(styles.generated, /\.api-main \.doc-diagram pre\s*\{[\s\S]*?overflow-x:\s*auto;/, "documentation diagrams must contain their own horizontal scrolling");

for (const [relativePath, surface] of [["wiki/site/en/api/GameObject.html", "API reference"]]) {
    const html = await read(relativePath);
    assert.match(html, viewportContract, `${surface} must opt into the device viewport`);
    assert.doesNotMatch(html, /\sstyle\s*=/i, `${surface} must not use inline layout styles`);
    assert.ok(html.includes('/css/wiki-generated.css?v=9'), `${surface} must load responsive generated-document styles`);
    assert.ok(html.includes(`/css/${hashedTemplateStyles[0]}`), `${surface} must load the current responsive template stylesheet`);
    assert.ok(html.includes('data-doc-outline'), `${surface} must retain its collapsible document outline`);
}

console.log("Responsive contract checks passed: static 375/768/1440 breakpoint coverage, device viewports, 44px touch targets, and 200% text-resilience rules across root and generated documentation surfaces.");
