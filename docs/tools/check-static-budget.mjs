import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";

const docsRoot = path.resolve("docs");
const errors = [];

const limits = {
  rootHtml: 64 * 1024,
  stylesheet: 64 * 1024,
  script: 96 * 1024,
  image: 1024 * 1024,
  webfont: 192 * 1024,
  machineIndex: 512 * 1024,
  rootExperience: 1250 * 1024,
  generatedWikiHtml: 96 * 1024,
  generatedWikiTotal: 8 * 1024 * 1024,
};
const rootRouteBudgets = new Map([
  ["index.html", 500 * 1024],
  ["start.html", 320 * 1024],
  ["learn.html", 320 * 1024],
  ["roadmap.html", 300 * 1024],
  ["community.html", 360 * 1024],
  ["download.html", 320 * 1024],
]);

async function files(directory) {
  return (await readdir(directory, { withFileTypes: true }))
    .filter((entry) => entry.isFile())
    .map((entry) => path.join(directory, entry.name));
}

async function recursiveFiles(directory) {
  const output = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const target = path.join(directory, entry.name);
    if (entry.isDirectory()) output.push(...await recursiveFiles(target));
    else if (entry.isFile()) output.push(target);
  }
  return output;
}

async function size(file) {
  return (await stat(file)).size;
}

function human(bytes) {
  return `${(bytes / 1024).toFixed(1)} KiB`;
}

async function enforce(file, limit, label) {
  const bytes = await size(file);
  if (bytes > limit) errors.push(`${path.relative(process.cwd(), file)} is ${human(bytes)}; ${label} budget is ${human(limit)}`);
  return bytes;
}

function localAsset(sourceFile, reference) {
  const clean = reference.trim().split("#", 1)[0].split("?", 1)[0];
  if (!clean || /^(?:[a-z]+:|\/\/)/i.test(clean)) return null;
  const target = clean.startsWith("/")
    ? path.join(docsRoot, clean.slice(1))
    : path.resolve(path.dirname(sourceFile), clean);
  const relative = path.relative(docsRoot, target);
  return relative.startsWith("..") || path.isAbsolute(relative) ? null : path.normalize(target);
}

async function rootRoutePayload(pageName) {
  const pageFile = path.join(docsRoot, pageName);
  const html = await readFile(pageFile, "utf8");
  const runtimeFiles = new Set();
  for (const match of html.matchAll(/\b(?:href|src)=["']([^"']+\.(?:css|js)(?:[?#][^"']*)?)["']/gi)) {
    const file = localAsset(pageFile, match[1]);
    if (file) runtimeFiles.add(file);
  }

  for (const cssFile of [...runtimeFiles].filter((file) => file.endsWith(".css"))) {
    const css = await readFile(cssFile, "utf8");
    for (const match of css.matchAll(/url\(\s*["']?([^"')]+)["']?\s*\)/gi)) {
      const file = localAsset(cssFile, match[1]);
      if (file) runtimeFiles.add(file);
    }
  }

  let bytes = await size(pageFile);
  for (const file of runtimeFiles) bytes += await size(file);
  const deliveredImages = new Set();

  const pictureBlocks = [...html.matchAll(/<picture\b[\s\S]*?<\/picture>/gi)].map((match) => match[0]);
  for (const picture of pictureBlocks) {
    const candidates = [];
    for (const match of picture.matchAll(/\b(?:src|srcset)=["']([^"']+)["']/gi)) {
      for (const candidate of match[1].split(",")) {
        const file = localAsset(pageFile, candidate.trim().split(/\s+/, 1)[0]);
        if (file) candidates.push(file);
      }
    }
    if (candidates.length) {
      const uniqueCandidates = [...new Set(candidates)];
      bytes += Math.max(...await Promise.all(uniqueCandidates.map(size)));
      uniqueCandidates.forEach((file) => deliveredImages.add(file));
    }
  }

  const htmlOutsidePictures = pictureBlocks.reduce((source, picture) => source.replace(picture, ""), html);
  for (const match of htmlOutsidePictures.matchAll(/<img\b[^>]*\bsrc=["']([^"']+)["'][^>]*>/gi)) {
    const file = localAsset(pageFile, match[1]);
    if (file && !deliveredImages.has(file)) {
      deliveredImages.add(file);
      bytes += await size(file);
    }
  }
  return bytes;
}

let rootExperience = 0;
for (const file of (await files(docsRoot)).filter((file) => file.endsWith(".html"))) {
  rootExperience += await enforce(file, limits.rootHtml, "root HTML");
}
for (const file of await files(path.join(docsRoot, "css"))) {
  rootExperience += await enforce(file, limits.stylesheet, "stylesheet");
}
for (const file of await files(path.join(docsRoot, "js"))) {
  rootExperience += await enforce(file, limits.script, "script");
}
rootExperience += await enforce(path.join(docsRoot, "sw.js"), limits.script, "service worker");
const responsiveImageSets = [
  ["demo-0.3.4.webp", "demo-0.3.4.avif"],
];
// The original PNG remains under docs/assets because the repository README uses
// it as review evidence. It is not referenced by a website page and therefore
// is not a browser-delivery candidate in the root experience budget.
const evidenceOnlyImages = new Set(["demo.png"]);
const groupedImages = new Set(responsiveImageSets.flat());
const imageSizes = new Map();
for (const file of (await files(path.join(docsRoot, "assets"))).filter((file) => /\.(?:avif|gif|jpe?g|png|webp)$/i.test(file))) {
  const name = path.basename(file);
  const bytes = evidenceOnlyImages.has(name)
    ? (await stat(file)).size
    : await enforce(file, limits.image, "image");
  imageSizes.set(name, bytes);
  if (!groupedImages.has(name) && !evidenceOnlyImages.has(name)) rootExperience += bytes;
}
for (const imageSet of responsiveImageSets) {
  const missing = imageSet.filter((name) => !imageSizes.has(name));
  if (missing.length) {
    errors.push(`responsive image set is missing ${missing.join(", ")}`);
    continue;
  }
  rootExperience += Math.max(...imageSet.map((name) => imageSizes.get(name)));
}
for (const file of (await files(path.join(docsRoot, "assets", "fonts"))).filter((file) => /\.(?:woff2?|ttf|otf)$/i.test(file))) {
  rootExperience += await enforce(file, limits.webfont, "webfont");
}
const routePayloads = new Map();
for (const [pageName, limit] of rootRouteBudgets) {
  const bytes = await rootRoutePayload(pageName);
  routePayloads.set(pageName, bytes);
  if (bytes > limit) errors.push(`${pageName} first-view payload is ${human(bytes)}; route budget is ${human(limit)}`);
}
for (const name of ["api-index.json", "api-changes.json", "release-notes.json"]) {
  await enforce(path.join(docsRoot, name), limits.machineIndex, "machine index");
}
await enforce(path.join(docsRoot, "sitemap.xml"), limits.machineIndex, "unified sitemap");
const generatedWikiFiles = await recursiveFiles(path.join(docsRoot, "wiki", "site"));
let generatedWikiTotal = 0;
for (const file of generatedWikiFiles) {
  generatedWikiTotal += file.endsWith(".html")
    ? await enforce(file, limits.generatedWikiHtml, "generated Wiki HTML")
    : await size(file);
}
if (generatedWikiTotal > limits.generatedWikiTotal) {
  errors.push(`generated Wiki total ${human(generatedWikiTotal)}; generated site budget is ${human(limits.generatedWikiTotal)}`);
}

if (rootExperience > limits.rootExperience) {
  errors.push(`root HTML/CSS/JS/images total ${human(rootExperience)}; experience budget is ${human(limits.rootExperience)}`);
}

if (errors.length) {
  console.error(`Static performance budget failed with ${errors.length} issue(s):`);
  for (const error of errors) console.error(`- ${error}`);
  process.exit(1);
}

const routeSummary = [...routePayloads].map(([page, bytes]) => `${page.replace(".html", "")} ${human(bytes)}`).join(", ");
console.log(`Static performance budget passed: route payloads ${routeSummary}; production surface ${human(rootExperience)} / ${human(limits.rootExperience)}; generated Wiki ${human(generatedWikiTotal)} / ${human(limits.generatedWikiTotal)}.`);
