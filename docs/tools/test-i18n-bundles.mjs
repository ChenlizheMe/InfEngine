import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import vm from "node:vm";

const docsRoot = path.resolve("docs");
const core = await readFile(path.join(docsRoot, "js", "i18n.js"), "utf8");
const source = JSON.parse(await readFile(path.join(docsRoot, "tools", "i18n-source.json"), "utf8"));
const routeKeys = {
    "404": "error.title",
    index: "home.hero.title",
    start: "pageTitle.start",
    roadmap: "roadmap.hero.title",
    download: "pageTitle.download",
};

for (const [page, pageKey] of Object.entries(routeKeys)) {
    const listeners = new Map();
    const sandbox = {
        CustomEvent: class CustomEvent {
            constructor(type, options) { this.type = type; this.detail = options?.detail; }
        },
        document: {
            addEventListener(type, handler) { listeners.set(type, handler); },
            dispatchEvent() {},
            documentElement: { getAttribute() { return null; }, lang: "en" },
            getElementById() { return null; },
            querySelectorAll() { return []; },
        },
        globalThis: null,
        localStorage: {
            getItem() { return "en"; },
            setItem() {},
        },
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    const pageBundle = await readFile(path.join(docsRoot, "js", `i18n-${page}.js`), "utf8");
    new vm.Script(pageBundle, { filename: `i18n-${page}.js` }).runInContext(sandbox);
    new vm.Script(core, { filename: "i18n.js" }).runInContext(sandbox);

    const translate = (key, language) => vm.runInContext(`translateSiteKey(${JSON.stringify(key)}, ${JSON.stringify(language)})`, sandbox);
    for (const language of ["en", "zh"]) {
        assert.equal(translate("nav.start", language), source[language]["nav.start"], `${page}: shared navigation copy must resolve in ${language}`);
        assert.equal(translate(pageKey, language), source[language][pageKey], `${page}: route copy must resolve in ${language}`);
    }
    const foreignKey = page === "index" ? routeKeys.download : routeKeys.index;
    assert.equal(translate(foreignKey, "en"), "", `${page}: another route's copy must not be delivered`);
    assert.equal("INFERNUX_PAGE_TRANSLATIONS" in sandbox, false, `${page}: staging data should be released after merge`);
    assert.equal(typeof listeners.get("DOMContentLoaded"), "function", `${page}: localization runtime should initialize after parsing`);
    listeners.get("DOMContentLoaded")();
}

{
    const listeners = new Map();
    const writes = [];
    const sandbox = {
        CustomEvent: class CustomEvent {
            constructor(type, options) { this.type = type; this.detail = options?.detail; }
        },
        URLSearchParams,
        window: { location: { search: "?lang=zh" } },
        document: {
            addEventListener(type, handler) { listeners.set(type, handler); },
            dispatchEvent() {},
            documentElement: { getAttribute() { return null; }, lang: "en" },
            getElementById() { return null; },
            querySelectorAll() { return []; },
        },
        globalThis: null,
        localStorage: {
            getItem() { return "en"; },
            setItem(key, value) { writes.push([key, value]); },
        },
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    const pageBundle = await readFile(path.join(docsRoot, "js", "i18n-index.js"), "utf8");
    new vm.Script(pageBundle, { filename: "i18n-index.js" }).runInContext(sandbox);
    new vm.Script(core, { filename: "i18n.js" }).runInContext(sandbox);
    listeners.get("DOMContentLoaded")();
    assert.equal(sandbox.document.documentElement.lang, "zh-CN", "an explicit ?lang=zh deep link must override an older local preference");
    assert.deepEqual(writes.at(-1), ["lang", "zh"], "the explicit deep-link language should become the current local preference");
}

assert.ok(Buffer.byteLength(core) < 8 * 1024, "shared localization runtime should stay below 8 KiB");
for (const obsolete of ["nav.home", "nav.features", "nav.showcase", "hero.roadmap", "wiki.library.loading"]) {
    assert.equal(Object.hasOwn(source.en, obsolete), false, `unused key '${obsolete}' should stay removed`);
}

console.log("Localization bundle test passed: route bundles load shared navigation plus only their own bilingual copy, explicit URL language wins, and the shared runtime stays below 8 KiB.");
