import { readFile, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const docsRoot = path.resolve("docs");
const outputFile = path.join(docsRoot, "sitemap.xml");
const check = process.argv.includes("--check");
const manifest = JSON.parse(await readFile(path.join(docsRoot, "docs-manifest.json"), "utf8"));
const apiIndex = JSON.parse(await readFile(path.join(docsRoot, "api-index.json"), "utf8"));
const origin = manifest.canonical_origin.replace(/\/$/, "");
const defaultLastmod = manifest.last_verified;
const rootPages = ["index.html", "start.html", "learn.html", "learn/placeholder.html", "roadmap.html", "download.html"];
const entries = new Map();

function escapeXml(value) {
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&apos;");
}

function addEntry(url, { lastmod = defaultLastmod, counterpart = null, language = null } = {}) {
    if (!url.startsWith(`${origin}/`) && url !== `${origin}/`) throw new Error(`Sitemap URL is outside the canonical origin: ${url}`);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(lastmod || "")) throw new Error(`Invalid lastmod '${lastmod}' for ${url}`);
    if (entries.has(url)) throw new Error(`Duplicate sitemap URL: ${url}`);
    entries.set(url, { url, lastmod, counterpart, language });
}

for (const file of rootPages) {
    const html = await readFile(path.join(docsRoot, file), "utf8");
    const canonical = html.match(/<link\s+rel=["']canonical["']\s+href=["']([^"']+)["']/i)?.[1];
    if (!canonical) throw new Error(`${file} is missing a canonical URL.`);
    addEntry(canonical);
}

addEntry(`${origin}/wiki/site/index.html`);
addEntry(`${origin}/wiki/site/en/api/index.html`, {
    language: "en",
    counterpart: `${origin}/wiki/site/zh/api/index.html`
});
addEntry(`${origin}/wiki/site/zh/api/index.html`, {
    language: "zh-CN",
    counterpart: `${origin}/wiki/site/en/api/index.html`
});

const apiUrls = new Set(apiIndex.symbols.map((symbol) => symbol.canonical_url));
for (const symbol of apiIndex.symbols) {
    const counterpart = `${origin}${symbol.counterpart_url}`;
    if (!apiUrls.has(counterpart)) throw new Error(`Missing sitemap counterpart for ${symbol.id}: ${counterpart}`);
    addEntry(symbol.canonical_url, {
        language: symbol.language,
        counterpart
    });
}

for (const entry of entries.values()) {
    const pathname = new URL(entry.url).pathname;
    const target = pathname === "/" ? path.join(docsRoot, "index.html") : path.join(docsRoot, pathname.slice(1));
    try {
        if (!(await stat(target)).isFile()) throw new Error("not a file");
    } catch {
        throw new Error(`Sitemap target does not exist: ${pathname}`);
    }
    if (entry.counterpart && !entries.has(entry.counterpart)) throw new Error(`Sitemap counterpart is not indexed: ${entry.counterpart}`);
}

const ordered = [...entries.values()].sort((a, b) => {
    if (a.url === `${origin}/`) return -1;
    if (b.url === `${origin}/`) return 1;
    return a.url.localeCompare(b.url, "en");
});

const xml = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">',
    ...ordered.flatMap((entry) => {
        const lines = ["  <url>", `    <loc>${escapeXml(entry.url)}</loc>`, `    <lastmod>${entry.lastmod}</lastmod>`];
        if (entry.counterpart) {
            const english = entry.language === "en" ? entry.url : entry.counterpart;
            const chinese = entry.language === "zh-CN" ? entry.url : entry.counterpart;
            lines.push(
                `    <xhtml:link rel="alternate" hreflang="en" href="${escapeXml(english)}" />`,
                `    <xhtml:link rel="alternate" hreflang="zh-CN" href="${escapeXml(chinese)}" />`,
                `    <xhtml:link rel="alternate" hreflang="x-default" href="${escapeXml(english)}" />`
            );
        }
        lines.push("  </url>");
        return lines;
    }),
    "</urlset>",
    ""
].join("\n");

let current = null;
try {
    current = await readFile(outputFile, "utf8");
} catch (error) {
    if (error.code !== "ENOENT") throw error;
}

const localizedCount = ordered.filter((entry) => entry.counterpart).length;
if (check) {
    if (current !== xml) throw new Error("sitemap.xml is stale. Run: node docs/tools/build-sitemap.mjs");
    console.log(`Verified unified sitemap with ${ordered.length} URLs and ${localizedCount} localized entries.`);
} else {
    await writeFile(outputFile, xml, "utf8");
    console.log(`Generated unified sitemap with ${ordered.length} URLs and ${localizedCount} localized entries.`);
}
