import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import vm from "node:vm";

const docsRoot = path.resolve("docs");
const source = await readFile(path.join(docsRoot, "js", "download.js"), "utf8");
const html = await readFile(path.join(docsRoot, "download.html"), "utf8");
const catalog = JSON.parse(await readFile(path.join(docsRoot, "hub-catalog.json"), "utf8"));
const listeners = new Map();

function makeSelect(id, initialUrl) {
    const link = { href: "" };
    const options = [{ value: initialUrl, textContent: "0.3.7" }];
    const select = {
        id,
        value: initialUrl,
        options,
        closest(selector) {
            if (selector === ".version-picker") return { querySelector() { return link; } };
            return null;
        },
        addEventListener(type, handler) { listeners.set(`${id}:${type}`, handler); }
    };
    return { select, link };
}

function makeLink() {
    const classes = new Set(["is-disabled"]);
    return {
        href: "",
        attributes: new Map(),
        classList: {
            add(name) { classes.add(name); },
            remove(name) { classes.delete(name); },
            contains(name) { return classes.has(name); }
        },
        removeAttribute(name) {
            this.attributes.delete(name);
            if (name === "href") this.href = "";
        },
        setAttribute(name, value) { this.attributes.set(name, value); }
    };
}

function makeLabel(chinese) {
    return {
        textContent: "",
        closest(selector) {
            return selector === "[data-page-language='zh']" && chinese ? {} : null;
        }
    };
}

const wheel = "https://example.invalid/infernux-0.3.7.whl";
const en = makeSelect("engine-version-en", wheel);
const zh = makeSelect("engine-version-zh", wheel);
const windowsLinks = [makeLink(), makeLink()];
const linuxLinks = [makeLink(), makeLink()];
const windowsLabels = [makeLabel(false), makeLabel(true)];
const linuxLabels = [makeLabel(false), makeLabel(true)];

const sandbox = {
    Array,
    Boolean,
    Error,
    Promise,
    String,
    fetch: async (url, options) => {
        assert.equal(url, "hub-catalog.json");
        assert.equal(options.cache, "no-store");
        return { ok: true, status: 200, async json() { return catalog; } };
    },
    document: {
        addEventListener(type, handler) { listeners.set(type, handler); },
        querySelectorAll(selector) {
            if (selector === "[data-version-select]") return [en.select, zh.select];
            if (selector === "[data-hub-link='windows-x64']") return windowsLinks;
            if (selector === "[data-hub-link='linux-x64']") return linuxLinks;
            if (selector === "[data-hub-meta='windows-x64']") return windowsLabels;
            if (selector === "[data-hub-meta='linux-x64']") return linuxLabels;
            return [];
        }
    }
};

vm.createContext(sandbox);
new vm.Script(source, { filename: "download.js" }).runInContext(sandbox);
listeners.get("DOMContentLoaded")();
await new Promise((resolve) => setImmediate(resolve));

const windowsInstaller = catalog.releases[0].platforms["windows-x64"].installer.url;
assert.equal(en.link.href, wheel);
assert.equal(zh.link.href, wheel);
assert.deepEqual(windowsLinks.map((link) => link.href), [windowsInstaller, windowsInstaller]);
assert.equal(windowsLinks[0].classList.contains("is-disabled"), false);
assert.equal(linuxLinks[0].href, "");
assert.equal(linuxLinks[0].attributes.get("aria-disabled"), "true");
assert.match(windowsLabels[0].textContent, /latest public release 0\.3\.7/);
assert.match(windowsLabels[1].textContent, /最新公开版本 0\.3\.7/);
assert.match(linuxLabels[0].textContent, /not publicly released yet/);
assert.match(linuxLabels[1].textContent, /尚未公开发布/);

assert.match(html, /InfernuxHub/, "the primary download must be presented as InfernuxHub");
assert.match(html, /data-hub-link="windows-x64"/, "the Windows Hub must have its own path");
assert.match(html, /data-hub-link="linux-x64"/, "the Linux Hub must have its own path");
assert.doesNotMatch(html, /api\.github\.com/, "the download page must not depend on GitHub's API");
assert.match(html, /<details class="advanced-download">/, "manual wheel downloads must live in advanced mode");
assert.doesNotMatch(html, /<details class="advanced-download"\s+open/, "advanced mode must be collapsed by default");
assert.match(html, /0\.2\.9[\s\S]*0\.2\.1[\s\S]*0\.2\.0/, "the direct wheel list should offer multiple versions");
assert.doesNotMatch(html, /SHA-?256|checksum|校验码|publisher signature/i, "ordinary downloads should not expose verification clutter");
assert.doesNotMatch(html, /pwa-install\.js/, "the download page should not load the documentation-app installer");

console.log("Download page test passed: the anonymous Hub catalog drives separate Windows and Linux paths.");
