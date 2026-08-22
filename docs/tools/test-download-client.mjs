import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import vm from "node:vm";

const docsRoot = path.resolve("docs");
const source = await readFile(path.join(docsRoot, "js", "download.js"), "utf8");
const html = await readFile(path.join(docsRoot, "download.html"), "utf8");
const listeners = new Map();

function makeSelect(id, initialUrl) {
    const link = { href: "" };
    const options = [{ value: initialUrl, textContent: "0.3.4 · fallback" }];
    const select = {
        id,
        value: initialUrl,
        options,
        ownerDocument: { createElement() { return { value: "", textContent: "" }; } },
        get firstChild() { return options[0] || null; },
        closest(selector) {
            if (selector === ".version-picker") return { querySelector() { return link; } };
            return null;
        },
        querySelectorAll(selector) { return selector === "option" ? options : []; },
        insertBefore(option) { options.unshift(option); },
        replaceChildren(...nextOptions) {
            options.splice(0, options.length, ...nextOptions);
        },
        addEventListener(type, handler) { listeners.set(`${id}:${type}`, handler); }
    };
    return { select, link };
}

const oldWheel = "https://github.com/ChenlizheMe/Infernux/releases/download/v0.3.4/infernux-0.3.4-cp312-cp312-win_amd64.whl";
const latestWheel = "https://github.com/ChenlizheMe/Infernux/releases/download/v0.3.7/infernux-0.3.7-cp312-cp312-win_amd64.whl";
const latestHub = "https://github.com/ChenlizheMe/Infernux/releases/download/v0.3.7/InfernuxHubInstaller-0.3.7.exe";
const previousWheel = "https://github.com/ChenlizheMe/Infernux/releases/download/v0.3.6/infernux-0.3.6-cp312-cp312-win_amd64.whl";
const latestRelease = {
    tag_name: "v0.3.7",
    assets: [
        { name: "InfernuxHubInstaller-0.3.7.exe", browser_download_url: latestHub },
        { name: "infernux-0.3.7-cp312-cp312-win_amd64.whl", browser_download_url: latestWheel }
    ]
};
const previousRelease = {
    tag_name: "v0.3.6",
    assets: [
        { name: "infernux-0.3.6-cp312-cp312-win_amd64.whl", browser_download_url: previousWheel }
    ]
};
const en = makeSelect("engine-version-en", oldWheel);
const zh = makeSelect("engine-version-zh", oldWheel);
const hubLinks = [{ href: "" }, { href: "" }];
const labels = [
    { textContent: "", closest() { return null; } },
    { textContent: "", closest() { return {}; } }
];

const sandbox = {
    Array,
    Error,
    Promise,
    String,
    fetch: async (url) => ({
        ok: true,
        status: 200,
        async json() {
            return String(url).includes("?per_page=")
                ? [latestRelease, previousRelease, { tag_name: "v0.4.0-rc1", prerelease: true, assets: [] }]
                : latestRelease;
        }
    }),
    document: {
        addEventListener(type, handler) { listeners.set(type, handler); },
        querySelectorAll(selector) {
            if (selector === "[data-version-select]") return [en.select, zh.select];
            if (selector === "[data-latest-hub]") return hubLinks;
            if (selector === "[data-hub-meta]") return labels;
            return [];
        }
    }
};

vm.createContext(sandbox);
new vm.Script(source, { filename: "download.js" }).runInContext(sandbox);
listeners.get("DOMContentLoaded")();
await new Promise((resolve) => setImmediate(resolve));

assert.equal(en.link.href, latestWheel, "the English wheel button should use GitHub's latest release");
assert.equal(zh.link.href, latestWheel, "the Chinese wheel button should use GitHub's latest release");
assert.equal(en.select.options[0].textContent, "0.3.7 · latest public release");
assert.equal(zh.select.options[0].textContent, "0.3.7 · 最新公开版本");
assert.deepEqual(en.select.options.map((option) => option.textContent), ["0.3.7 · latest public release", "0.3.6"]);
assert.equal(en.select.options[1].value, previousWheel, "published GitHub wheels should populate the version list");
assert.deepEqual(hubLinks.map((link) => link.href), [latestHub, latestHub]);
assert.match(labels[0].textContent, /latest public release 0\.3\.7/);
assert.match(labels[1].textContent, /最新公开版本 0\.3\.7/);

en.select.value = oldWheel;
listeners.get("engine-version-en:change")();
assert.equal(en.link.href, oldWheel, "changing versions should still update the direct wheel link");

assert.match(html, /InfernuxHub/, "the primary download must be presented as InfernuxHub");
assert.match(html, /data-latest-hub/, "the recommended Hub download must track GitHub's latest release");
assert.match(html, /connect-src 'self' https:\/\/api\.github\.com/, "the download page CSP must allow the GitHub Releases API");
assert.match(html, /<details class="advanced-download">/, "manual wheel downloads must live in advanced mode");
assert.doesNotMatch(html, /<details class="advanced-download"\s+open/, "advanced mode must be collapsed by default");
assert.match(html, /0\.2\.9[\s\S]*0\.2\.1[\s\S]*0\.2\.0/, "the offline fallback should offer multiple engine versions");
assert.doesNotMatch(html, /SHA-?256|checksum|校验码|publisher signature/i, "ordinary downloads should not expose verification clutter");
assert.doesNotMatch(html, /pwa-install\.js/, "the download page should not load the documentation-app installer");

console.log("Download page test passed: GitHub latest drives recommended downloads with checked-in fallbacks.");
