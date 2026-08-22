import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const docsRoot = path.resolve(scriptDir, "..");
const repoRoot = path.resolve(docsRoot, "..");
const width = 1920;
const height = 1032;
const expected = {
    "demo.png": {
        type: "png",
        sha256: "4be6e30abfd71f3e4a31593ce6e44817cffdb83ec170cee489a3b40b162d3d91",
    },
    "demo-0.3.4.webp": {
        type: "webp",
        sha256: "10a141e9c795829ded555363d0866c1508403e19fb4fdc14401e1532f313384c",
    },
    "demo-0.3.4.avif": {
        type: "avif",
        sha256: "4cbe016a9eedfefebb8d7a2bbd107e829ca706f045de2aa4d5c381c456efa9f5",
    },
};
const failures = [];

function fail(message) {
    failures.push(message);
}

function pngDimensions(buffer) {
    if (buffer.subarray(0, 8).toString("hex") !== "89504e470d0a1a0a" || buffer.length < 24) return null;
    return { width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) };
}

function webpDimensions(buffer) {
    if (buffer.length < 25 || buffer.subarray(0, 4).toString("ascii") !== "RIFF" || buffer.subarray(8, 12).toString("ascii") !== "WEBP") return null;
    const chunk = buffer.subarray(12, 16).toString("ascii");
    if (chunk === "VP8L" && buffer[20] === 0x2f) {
        return {
            width: 1 + buffer[21] + ((buffer[22] & 0x3f) << 8),
            height: 1 + ((buffer[22] & 0xc0) >> 6) + (buffer[23] << 2) + ((buffer[24] & 0x0f) << 10),
        };
    }
    if (chunk === "VP8X" && buffer.length >= 30) {
        return {
            width: 1 + buffer.readUIntLE(24, 3),
            height: 1 + buffer.readUIntLE(27, 3),
        };
    }
    if (chunk === "VP8 " && buffer.length >= 30 && buffer.subarray(23, 26).toString("hex") === "9d012a") {
        return {
            width: buffer.readUInt16LE(26) & 0x3fff,
            height: buffer.readUInt16LE(28) & 0x3fff,
        };
    }
    return null;
}

function avifDimensions(buffer) {
    if (buffer.length < 32 || buffer.subarray(4, 8).toString("ascii") !== "ftyp") return null;
    const brands = buffer.subarray(8, Math.min(buffer.length, 64)).toString("ascii");
    if (!brands.includes("avif") && !brands.includes("avis")) return null;
    const marker = Buffer.from("ispe", "ascii");
    const offset = buffer.indexOf(marker);
    if (offset < 4 || offset + 16 > buffer.length) return null;
    return { width: buffer.readUInt32BE(offset + 8), height: buffer.readUInt32BE(offset + 12) };
}

const parsers = { png: pngDimensions, webp: webpDimensions, avif: avifDimensions };
const assets = new Map();
for (const [name, contract] of Object.entries(expected)) {
    const buffer = await readFile(path.join(docsRoot, "assets", name));
    assets.set(name, buffer);
    const hash = createHash("sha256").update(buffer).digest("hex");
    if (hash !== contract.sha256) fail(`${name}: content hash differs from the reviewed evidence variant`);
    const dimensions = parsers[contract.type](buffer);
    if (!dimensions) fail(`${name}: invalid ${contract.type.toUpperCase()} structure`);
    else if (dimensions.width !== width || dimensions.height !== height) fail(`${name}: expected ${width}x${height}, found ${dimensions.width}x${dimensions.height}`);
}

const pngBytes = assets.get("demo.png").length;
const webpBytes = assets.get("demo-0.3.4.webp").length;
const avifBytes = assets.get("demo-0.3.4.avif").length;
if (webpBytes >= pngBytes * 0.2) fail(`WebP must save at least 80% over PNG; found ${webpBytes} versus ${pngBytes} bytes`);
if (avifBytes >= pngBytes * 0.2) fail(`AVIF must save at least 80% over PNG; found ${avifBytes} versus ${pngBytes} bytes`);
if (webpBytes <= avifBytes) fail("AVIF should remain the smallest preferred representation");

const homepage = await readFile(path.join(docsRoot, "index.html"), "utf8");
const picture = homepage.match(/<picture>([\s\S]*?)<\/picture>/i)?.[1];
if (!picture) {
    fail("index.html: runtime evidence must use a picture element");
} else {
    const avifSource = '<source srcset="assets/demo-0.3.4.avif" type="image/avif">';
    const fallback = '<img src="assets/demo-0.3.4.webp" width="1920" height="1032" alt="Infernux 0.3.4 editor rendering the 65,536-object Voxel Continent showcase with a custom RenderStack" loading="lazy" decoding="async">';
    for (const token of [avifSource, fallback]) if (!picture.includes(token)) fail(`index.html: picture is missing '${token}'`);
    if (picture.includes('<source srcset="assets/demo-0.3.4.webp"')) fail("index.html: WebP should be the img fallback, not a redundant source candidate");
    if (!(picture.indexOf(avifSource) < picture.indexOf(fallback))) fail("index.html: picture sources must prefer AVIF and fall back to WebP");
}
if (!homepage.includes('"screenshot": "https://infernux-engine.com/assets/demo-0.3.4.webp"')) fail("index.html: structured evidence must use the delivered WebP screenshot");
if (homepage.includes("assets/demo.png")) fail("index.html: the repository-only PNG evidence source must not be part of website delivery");
if (/<link\b[^>]*rel=["']preload["'][^>]*demo-/i.test(homepage)) fail("index.html: below-the-fold runtime evidence must not compete with first-view content via preload");

const sharedCss = await readFile(path.join(docsRoot, "css", "style.css"), "utf8");
for (const contract of [".demo-frame picture", "aspect-ratio: 80 / 43", "width: 100%", "height: auto"]) {
    if (!sharedCss.includes(contract)) fail(`style.css: missing responsive evidence-image contract '${contract}'`);
}

const budget = await readFile(path.join(docsRoot, "tools", "check-static-budget.mjs"), "utf8");
for (const name of Object.keys(expected)) if (!budget.includes(`"${name}"`)) fail(`check-static-budget.mjs: responsive delivery set omits '${name}'`);
if (!budget.includes("Math.max(...imageSet")) fail("check-static-budget.mjs: responsive alternatives must count the worst delivered representation, not every mutually exclusive file");
if (!budget.includes("evidenceOnlyImages")) fail("check-static-budget.mjs: the retained PNG source must be separated from website delivery");

for (const readmeName of ["README.md", "README-zh.md"]) {
    const readme = await readFile(path.join(repoRoot, readmeName), "utf8");
    if (!readme.includes('src="docs/assets/demo.png"')) fail(`${readmeName}: repository evidence should retain the original PNG source`);
}

const provenance = await readFile(path.join(docsRoot, "assets", "VENDOR_ASSETS.md"), "utf8");
for (const contract of ["demo-0.3.4.webp", "demo-0.3.4.avif", "1920×1032", "Pillow 12.2.0", "quality 88"]) {
    if (!provenance.includes(contract)) fail(`VENDOR_ASSETS.md: missing reviewed image provenance '${contract}'`);
}

for (const workflow of ["website-quality.yml", "build-wiki.yml"]) {
    const source = await readFile(path.join(repoRoot, ".github", "workflows", workflow), "utf8");
    if (!source.includes("node docs/tools/check-image-variants.mjs")) fail(`${workflow}: responsive image gate is not part of the published workflow`);
}

if (failures.length) {
    console.error(`Responsive image audit failed with ${failures.length} issue(s):`);
    for (const failure of failures) console.error(`- ${failure}`);
    process.exit(1);
}

const savings = (bytes) => ((1 - bytes / pngBytes) * 100).toFixed(1);
console.log(
    `Responsive image audit passed: ${width}x${height}; `
    + `source PNG ${(pngBytes / 1024).toFixed(1)} KiB retained for repository evidence; `
    + `website WebP fallback ${(webpBytes / 1024).toFixed(1)} KiB (-${savings(webpBytes)}%); `
    + `preferred AVIF ${(avifBytes / 1024).toFixed(1)} KiB (-${savings(avifBytes)}%).`
);
