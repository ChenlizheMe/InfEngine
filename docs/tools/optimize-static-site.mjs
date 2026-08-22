import { createHash } from "node:crypto";
import { readFile, readdir, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const docsRoot = path.resolve("docs");
const root = path.resolve("docs/wiki/site");
const templateFile = path.join(docsRoot, "wiki", "theme", "main.html");
const cssRoot = path.join(docsRoot, "css");
const check = process.argv.includes("--check");
const changes = [];
const hardenedCsp = "default-src 'self'; base-uri 'self'; object-src 'none'; script-src 'self'; script-src-attr 'none'; style-src 'self'; style-src-attr 'none'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-src 'none'; worker-src 'self'; manifest-src 'self'; form-action 'self'; upgrade-insecure-requests";
const securityMeta = `<meta http-equiv="Content-Security-Policy" content="${hardenedCsp}">
    <meta name="referrer" content="strict-origin-when-cross-origin">`;
const unusedThemeOutputs = [
  path.join(root, "assets"),
  path.join(root, "search"),
];

async function walk(directory) {
  const files = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const target = path.join(directory, entry.name);
    if (entry.isDirectory()) files.push(...await walk(target));
    else if (entry.isFile()) files.push(target);
  }
  return files;
}

function normalizedCss(value) {
  return value.replace(/\r\n/g, "\n").trim() + "\n";
}

const textDiagramPattern = /<div class="language-text highlight"><pre><span><\/span><code>([\s\S]*?)<\/code><\/pre><\/div>/g;
const diagramMarkerPattern = /^\[INX-DIAGRAM:([a-z][a-z0-9-]*):([^\]]+)\]$/;

function enhanceTextDiagrams(html) {
  return html.replace(textDiagramPattern, (block, code) => {
    const lineBreak = code.indexOf("\n");
    if (lineBreak < 0) return block;

    const markerLine = code.slice(0, lineBreak);
    const markerText = markerLine.replace(/<[^>]+>/g, "").trim();
    const marker = markerText.match(diagramMarkerPattern);
    if (!marker) return block;

    const [, kind, label] = marker;
    const attributeLabel = label.replace(/"/g, "&quot;");
    const diagramCode = code.slice(lineBreak + 1);
    return `<figure class="doc-diagram doc-diagram--${kind}" role="group" aria-label="${attributeLabel}">`
      + `<figcaption><span>INX / SYSTEM MAP</span>${label}</figcaption>`
      + `<pre><code>${diagramCode}</code></pre></figure>`;
  });
}

function hardenGeneratedHtml(html) {
  let hardened = html;
  const cspMetaPattern = /<meta\s+http-equiv="Content-Security-Policy"\s+content="[^"]*"\s*\/?>/i;
  if (cspMetaPattern.test(hardened)) {
    hardened = hardened.replace(cspMetaPattern, `<meta http-equiv="Content-Security-Policy" content="${hardenedCsp}">`);
  } else {
    hardened = hardened.replace(
      /(<meta name="viewport" content="width=device-width, initial-scale=1\.0">)/,
      `$1\n    ${securityMeta}`
    );
  }
  return hardened
    .replace(/\s+onclick="toggleTheme\(\)"/g, ' data-site-action="theme"')
    .replace(/\s+onclick="toggleMobileMenu\(\)"/g, ' data-site-action="menu"')
        .replace(/\/js\/main\.js\?v=(?:9|10|11|12|13|14)/g, "/js/main.js?v=15")
        .replace(/\/js\/docs-search\.js\?v=(?:1|2|3|4|5)/g, "/js/docs-search.js?v=6");
}

const template = await readFile(templateFile, "utf8");
const templateStyleMatches = [...template.matchAll(/<style\b[^>]*>([\s\S]*?)<\/style>/gi)];
if (templateStyleMatches.length !== 1) {
  throw new Error(`Expected exactly one shared style block in ${path.relative(process.cwd(), templateFile)}, found ${templateStyleMatches.length}.`);
}
const sharedWikiCss = normalizedCss(templateStyleMatches[0][1]);
const sharedWikiCssHash = createHash("sha256").update(sharedWikiCss).digest("hex").slice(0, 16);
const sharedWikiCssName = `wiki-template.${sharedWikiCssHash}.css`;
const sharedWikiCssFile = path.join(cssRoot, sharedWikiCssName);
const sharedWikiCssHref = `/css/${sharedWikiCssName}`;
const sharedWikiCssLink = `<link rel="stylesheet" href="${sharedWikiCssHref}">`;
const hashedWikiCssPattern = /^wiki-template\.[a-f0-9]{16}\.css$/;

const existingWikiCss = (await readdir(cssRoot)).filter((name) => hashedWikiCssPattern.test(name));
for (const name of existingWikiCss) {
  if (name === sharedWikiCssName) continue;
  changes.push(`docs/css/${name} (stale shared Wiki style)`);
  if (!check) await rm(path.join(cssRoot, name));
}

let currentSharedCss = null;
try {
  currentSharedCss = await readFile(sharedWikiCssFile, "utf8");
} catch (error) {
  if (error.code !== "ENOENT") throw error;
}
if (currentSharedCss !== sharedWikiCss) {
  changes.push(`docs/css/${sharedWikiCssName} (shared Wiki style)`);
  if (!check) await writeFile(sharedWikiCssFile, sharedWikiCss, "utf8");
}

for (const directory of unusedThemeOutputs) {
  const relative = path.relative(process.cwd(), directory).split(path.sep).join("/");
  try {
    await readdir(directory);
    changes.push(`${relative}/ (unused Material runtime)`);
    if (!check) await rm(directory, { recursive: true, force: true });
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
}

for (const file of await walk(root)) {
  const relative = path.relative(process.cwd(), file).split(path.sep).join("/");
  if (file.endsWith(".map")) {
    changes.push(relative);
    if (!check) await rm(file);
    continue;
  }

  if (file.endsWith(".html")) {
    const source = await readFile(file, "utf8");
    let optimized = source;
    const styleMatches = [...optimized.matchAll(/<style\b[^>]*>([\s\S]*?)<\/style>/gi)];
    const inlineSharedStyle = styleMatches.find((match) => normalizedCss(match[1]) === sharedWikiCss);
    const hasSharedLink = optimized.includes(sharedWikiCssLink);
    const staleSharedLink = optimized.match(/<link\s+rel=["']stylesheet["']\s+href=["']\/css\/wiki-template\.[a-f0-9]{16}\.css["']\s*\/?>/i);

    if (inlineSharedStyle) {
      changes.push(`${relative} (inline shared Wiki style)`);
      optimized = optimized.slice(0, inlineSharedStyle.index)
        + sharedWikiCssLink
        + optimized.slice(inlineSharedStyle.index + inlineSharedStyle[0].length);
    } else if (staleSharedLink && !hasSharedLink) {
      changes.push(`${relative} (stale shared Wiki style link)`);
      optimized = optimized.replace(staleSharedLink[0], sharedWikiCssLink);
    } else if (!hasSharedLink) {
      changes.push(`${relative} (missing shared Wiki style link)`);
    }

    const diagramEnhanced = enhanceTextDiagrams(optimized);
    if (diagramEnhanced !== optimized) {
      changes.push(`${relative} (semantic text diagram)`);
      optimized = diagramEnhanced;
    }
    const hardened = hardenGeneratedHtml(optimized);
    if (hardened !== optimized) {
      changes.push(`${relative} (static CSP and external event bindings)`);
      optimized = hardened;
    }
    if (!check && optimized !== source) await writeFile(file, optimized, "utf8");
    continue;
  }

  if (file.endsWith(".js") || file.endsWith(".css")) {
    const source = await readFile(file, "utf8");
    const optimized = source
      .replace(/\n?\/\/# sourceMappingURL=[^\r\n*]+(?:\r?\n)?/g, "\n")
      .replace(/\n?\/\*# sourceMappingURL=[^*]+\*\/(?:\r?\n)?/g, "\n");
    if (optimized !== source) {
      changes.push(`${relative} (sourceMappingURL)`);
      if (!check) await writeFile(file, optimized, "utf8");
    }
  }
}

if (check && changes.length) {
  console.error("Static Wiki output still contains removable production artifacts:");
  for (const item of changes) console.error(`- ${item}`);
  process.exit(1);
}

console.log(check
  ? `Static Wiki output uses ${sharedWikiCssName} and has no removable production artifacts.`
  : `Optimized static Wiki output (${changes.length} production artifact(s) removed).`);
