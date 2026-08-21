import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import vm from "node:vm";

const docsRoot = path.resolve("docs");
const source = await readFile(path.join(docsRoot, "js", "docs-search.js"), "utf8");
const apiIndex = JSON.parse(await readFile(path.join(docsRoot, "api-index.json"), "utf8"));
const sandbox = {
    __INFERNUX_DOCS_SEARCH_TEST__: true,
    URLSearchParams,
    globalThis: null,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
new vm.Script(source, { filename: "docs-search.js" }).runInContext(sandbox);

const { buildSearchModel, buildWikiSearchUrl, search } = sandbox.__infernuxDocsSearch;
const model = buildSearchModel(apiIndex, null);
assert.equal(model.sources.api, true);
assert.equal(model.sources.docs, false);
assert.equal(model.items.length, apiIndex.symbols.length);
assert.ok(search(model, { query: "Camera", language: "en", layer: "api" }).total > 0);
assert.equal(buildWikiSearchUrl({ query: "Camera" }), "/wiki/site/en/api/index.html?q=Camera");
assert.equal(
    buildWikiSearchUrl({ query: "相机", language: "zh-CN", status: "stable" }),
    "/wiki/site/zh/api/index.html?q=%E7%9B%B8%E6%9C%BA&status=stable&lang=zh"
);
assert.doesNotMatch(source, /fetch\(["']\/docs-index\.json/, "search should not request the removed guide index");
assert.doesNotMatch(source, /\["learn"|\["manual"|\["architecture"/, "search filters should expose API only");

console.log("Documentation search test passed: generated API is the only search source.");
