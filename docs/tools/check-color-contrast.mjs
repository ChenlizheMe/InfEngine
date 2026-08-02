import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const docsRoot = path.resolve(scriptDir, "..");
const repoRoot = path.resolve(docsRoot, "..");
const styleFile = path.join(docsRoot, "css", "style.css");
const style = await readFile(styleFile, "utf8");
const failures = [];

function fail(message) {
    failures.push(message);
}

function tokenBlock(source, selector, label) {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const match = source.match(new RegExp(`${escaped}\\s*\\{([\\s\\S]*?)\\}`));
    if (!match) throw new Error(`Missing ${label} token block '${selector}'.`);
    return Object.fromEntries(
        [...match[1].matchAll(/--([a-z0-9-]+)\s*:\s*([^;]+);/gi)]
            .map((entry) => [entry[1], entry[2].trim()])
    );
}

function color(value, label) {
    const hex = value.match(/^#([a-f0-9]{3}|[a-f0-9]{6}|[a-f0-9]{8})$/i)?.[1];
    if (hex) {
        const normalized = hex.length === 3
            ? [...hex].map((part) => part + part).join("")
            : hex;
        return {
            red: Number.parseInt(normalized.slice(0, 2), 16),
            green: Number.parseInt(normalized.slice(2, 4), 16),
            blue: Number.parseInt(normalized.slice(4, 6), 16),
            alpha: normalized.length === 8 ? Number.parseInt(normalized.slice(6, 8), 16) / 255 : 1,
        };
    }

    const rgb = value.match(/^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*([\d.]+))?\s*\)$/i);
    if (rgb) {
        return {
            red: Number(rgb[1]),
            green: Number(rgb[2]),
            blue: Number(rgb[3]),
            alpha: rgb[4] === undefined ? 1 : Number(rgb[4]),
        };
    }

    throw new Error(`${label} must be a literal hex/rgb color, found '${value}'.`);
}

function composite(foreground, background) {
    if (background.alpha !== 1) throw new Error("Contrast backgrounds must resolve to opaque colors.");
    return {
        red: foreground.red * foreground.alpha + background.red * (1 - foreground.alpha),
        green: foreground.green * foreground.alpha + background.green * (1 - foreground.alpha),
        blue: foreground.blue * foreground.alpha + background.blue * (1 - foreground.alpha),
        alpha: 1,
    };
}

function luminance(channel) {
    const normalized = channel / 255;
    return normalized <= 0.04045
        ? normalized / 12.92
        : ((normalized + 0.055) / 1.055) ** 2.4;
}

function contrast(foreground, background) {
    const resolvedForeground = foreground.alpha === 1 ? foreground : composite(foreground, background);
    const foregroundLuminance = 0.2126 * luminance(resolvedForeground.red)
        + 0.7152 * luminance(resolvedForeground.green)
        + 0.0722 * luminance(resolvedForeground.blue);
    const backgroundLuminance = 0.2126 * luminance(background.red)
        + 0.7152 * luminance(background.green)
        + 0.0722 * luminance(background.blue);
    return (Math.max(foregroundLuminance, backgroundLuminance) + 0.05)
        / (Math.min(foregroundLuminance, backgroundLuminance) + 0.05);
}

const rootTokens = tokenBlock(style, ":root", "shared design");
const themes = {
    dark: rootTokens,
    light: { ...rootTokens, ...tokenBlock(style, '[data-theme="light"]', "shared design") },
};
const surfaces = ["bg", "bg-deep", "bg-panel", "bg-elevated", "bg-tile", "bg-contrast", "code-bg"];
const normalText = ["text", "text-muted", "text-soft", "text-mono", "accent", "accent-strong", "hazard", "signal", "info"];
const syntaxText = [
    "code-text-default",
    "code-hl-keyword",
    "code-hl-class",
    "code-hl-fn",
    "code-hl-param",
    "code-hl-type",
    "code-hl-string",
    "code-hl-number",
    "code-hl-comment",
    "code-hl-literal",
    "code-hl-decorator",
];
const tintedText = [
    ["accent", "accent-soft"],
    ["accent-strong", "accent-soft"],
    ["text-muted", "accent-soft"],
    ["hazard", "hazard-soft"],
    ["signal", "signal-soft"],
];
let checks = 0;
let minimumText = { ratio: Number.POSITIVE_INFINITY, label: "" };
let minimumUi = { ratio: Number.POSITIVE_INFINITY, label: "" };

function resolved(themeName, tokens, name) {
    if (!tokens[name]) {
        fail(`${themeName}: missing --${name}`);
        return null;
    }
    try {
        return color(tokens[name], `${themeName} --${name}`);
    } catch (error) {
        fail(error.message);
        return null;
    }
}

function requireRatio(themeName, tokens, foregroundName, backgroundName, threshold, kind, backgroundOverride = null) {
    const foreground = resolved(themeName, tokens, foregroundName);
    const background = backgroundOverride || resolved(themeName, tokens, backgroundName);
    if (!foreground || !background) return;
    const ratio = contrast(foreground, background);
    const label = `${themeName} --${foregroundName} / --${backgroundName}`;
    const minimum = kind === "text" ? minimumText : minimumUi;
    if (ratio < minimum.ratio) {
        minimum.ratio = ratio;
        minimum.label = label;
    }
    checks += 1;
    if (ratio + Number.EPSILON < threshold) fail(`${label} is ${ratio.toFixed(2)}:1; expected at least ${threshold.toFixed(1)}:1`);
}

for (const [themeName, tokens] of Object.entries(themes)) {
    for (const foreground of normalText) {
        for (const background of surfaces) requireRatio(themeName, tokens, foreground, background, 4.5, "text");
    }

    const codeBackground = resolved(themeName, tokens, "code-bg");
    if (codeBackground) {
        for (const foreground of syntaxText) requireRatio(themeName, tokens, foreground, "code-bg", 4.5, "text", codeBackground);
    }

    for (const [foreground, tint] of tintedText) {
        const tintColor = resolved(themeName, tokens, tint);
        if (!tintColor) continue;
        for (const surface of surfaces) {
            const surfaceColor = resolved(themeName, tokens, surface);
            if (!surfaceColor) continue;
            requireRatio(themeName, tokens, foreground, `${tint} over ${surface}`, 4.5, "text", composite(tintColor, surfaceColor));
        }
    }

    for (const fill of ["accent-fill", "accent-fill-hover"]) requireRatio(themeName, tokens, "on-accent", fill, 4.5, "text");
    for (const status of ["hazard", "signal"]) requireRatio(themeName, tokens, "bg-deep", status, 4.5, "text");
    for (const surface of surfaces) requireRatio(themeName, tokens, "border-strong", surface, 3, "ui");
}

requireRatio("print", rootTokens, "print-ink", "print-paper", 7, "text");
for (const rule of ["print-rule", "print-grid"]) requireRatio("print", rootTokens, rule, "print-paper", 3, "ui");

const offlineSource = await readFile(path.join(docsRoot, "offline.html"), "utf8");
const offlineTokens = tokenBlock(offlineSource, ":root", "offline-page");
const offlineSurfaces = ["offline-bg", "offline-panel", "offline-elevated"];
for (const foreground of ["offline-text", "offline-muted", "offline-soft", "offline-accent"]) {
    for (const background of offlineSurfaces) requireRatio("offline", offlineTokens, foreground, background, 4.5, "text");
}
requireRatio("offline", offlineTokens, "offline-on-accent", "offline-accent-fill", 4.5, "text");
for (const background of offlineSurfaces) {
    requireRatio("offline", offlineTokens, "offline-focus", background, 3, "ui");
    requireRatio("offline", offlineTokens, "offline-border", background, 3, "ui");
}

if (rootTokens["text-faint"] || themes.light["text-faint"]) fail("Low-contrast ornament must use --decorative-faint, not a misleading --text-faint token.");

const foregroundSources = [
    "css/style.css",
    "css/mission.css",
    "css/start.css",
    "css/download.css",
    "css/roadmap.css",
    "css/docs-search.css",
    "css/wiki-generated.css",
    "wiki/theme/main.html",
    "offline.html",
];
for (const relative of foregroundSources) {
    const source = await readFile(path.join(docsRoot, relative), "utf8");
    if (/(?:^|[;{]\s*|\n\s*)color\s*:\s*(?:#[a-f0-9]{3,8}\b|rgba?\()/gim.test(source)) {
        fail(`${relative}: readable foreground colors must use audited semantic tokens, not component-local literals`);
    }
}

for (const workflow of ["website-quality.yml", "build-wiki.yml"]) {
    const source = await readFile(path.join(repoRoot, ".github", "workflows", workflow), "utf8");
    if (!source.includes("node docs/tools/check-color-contrast.mjs")) fail(`${workflow}: color contrast gate is not part of the published workflow`);
}

if (failures.length) {
    console.error(`Color contrast audit failed with ${failures.length} issue(s):`);
    for (const failure of failures) console.error(`- ${failure}`);
    process.exit(1);
}

console.log(
    `Color contrast passed ${checks} contracts across dark/light themes and the offline recovery page; `
    + `minimum text ${minimumText.ratio.toFixed(2)}:1 (${minimumText.label}); `
    + `minimum UI boundary ${minimumUi.ratio.toFixed(2)}:1 (${minimumUi.label}).`
);
