import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const docsRoot = path.resolve("docs");
const check = process.argv.includes("--check");
const pages = ["index.html", "start.html", "learn.html", "roadmap.html", "download.html", "404.html"];
// Nested same-tag markup defeats the closing-tag match, so these two stay hand-maintained.
const handWritten = new Set(["brand.ribbonName", "home.hero.title"]);

const source = JSON.parse(await readFile(path.join(docsRoot, "tools", "i18n-source.json"), "utf8"));
const stale = [];
let normalized = 0;

for (const page of pages) {
    const file = path.join(docsRoot, page);
    const original = await readFile(file, "utf8");
    let html = original;
    for (const [key, english] of Object.entries(source.en)) {
        if (handWritten.has(key)) continue;
        const pattern = new RegExp(`(<(\\w+)(?:\\s[^>]*)?\\sdata-i18n="${key.replaceAll(".", "\\.")}"(?:\\s[^>]*)?>)([\\s\\S]*?)(</\\2>)`, "g");
        html = html.replace(pattern, (match, open, tag, body, close) => (body.includes(`<${tag}`) ? match : `${open}${english}${close}`));
    }
    if (html === original) continue;
    if (check) {
        stale.push(page);
        continue;
    }
    await writeFile(file, html, "utf8");
    normalized += 1;
}

if (stale.length) {
    console.error(`Inline English fallbacks are stale in ${stale.join(", ")}; run node docs/tools/normalize-i18n-fallbacks.mjs.`);
    process.exit(1);
}

console.log(check
    ? `Verified inline English fallbacks against i18n-source.json across ${pages.length} routes.`
    : `Normalized inline English fallbacks in ${normalized} of ${pages.length} routes.`);
