import process from "node:process";
import { appendFile, mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { buildWebsiteHealthReport, renderWebsiteHealthSummary } from "./website-health-report.mjs";

const baseArg = process.argv.indexOf("--base-url");
const base = new URL(baseArg >= 0 ? process.argv[baseArg + 1] : "https://infernux-engine.com/");
const reportArg = process.argv.indexOf("--report");
const reportPath = reportArg >= 0 ? path.resolve(process.argv[reportArg + 1]) : null;
const allowUnstamped = process.argv.includes("--allow-unstamped");
const failures = [];
const healthResults = [];
const startedAt = new Date();
let deployedManifest = null;
const requestAttempts = 3;
const requestTimeoutMs = 20_000;

const checks = [
    { route: "/", tokens: ["<h1", "start.html", "https://infernux-engine.discourse.group/"] },
    { route: "/start.html", tokens: ["data-page-language=\"en\"", "data-page-language=\"zh\"", "id=\"first-script\""], forbid: ["始于", "验证于", "nav.manual"] },
    { route: "/learn.html", tokens: ["learn/gameplay.html", "learn/rendering.html", "learn-course-grid"] },
    { route: "/learn/gameplay.html", tokens: ["data-learn-search", "data-learn-tag", "Build gameplay with Python"] },
    { route: "/learn/rendering.html", tokens: ["data-learn-search", "data-learn-tag", "Author the rendering pipeline"] },
    { route: "/learn/rendering-overview.html", tokens: ["Material", "RenderPipeline", "rendering-overview.md"] },
    { route: "/learn/vertex-stage.html", tokens: ["vertex()", "ShaderInfo", "vertex-stage.md"] },
    { route: "/learn/fragment-materials.html", tokens: ["SurfaceData", "Queue", "fragment-materials.md"] },
    { route: "/learn/shading-models-glsl.html", tokens: ["void shading", "Unsupported [Deferred]", "shading-models-glsl.md"] },
    { route: "/learn/render-effects.html", tokens: ["RenderEffect", "FullScreenEffect", "render-effects.md"] },
    { route: "/learn/renderstack-mount-points.html", tokens: ["after_camera_ui", "after_screen_ui", "renderstack-mount-points.md"] },
    { route: "/learn/custom-render-pipelines.html", tokens: ["MixedArtPipeline", "forward_plus", "custom-render-pipelines.md"] },
    { route: "/learn/rendergraph-advanced.html", tokens: ["define_topology", "PassResult", "rendergraph-advanced.md"] },
    { route: "/download.html", tokens: ["InfernuxHub", "advanced-download", "data-version-select", ".whl", "0.3.5", "0.3.4", "0.2.9"], forbid: ["SHA-256", "checksum", "校验码", "pwa-install.js", "advanced-download\" open"] },
    { route: "/community.html", tokens: ["https://infernux-engine.discourse.group/", "http-equiv=\"refresh\""] },
    { route: "/roadmap.html", tokens: ["<h1", "start.html"] },
    { route: "/wiki/site/en/api/index.html", tokens: ["API", "/start.html", "/learn.html"], forbid: [">Manual</a>", "/manual/"] },
    { route: "/wiki/site/zh/api/index.html", tokens: ["API", "/start.html", "/learn.html"], forbid: [">手册</a>", "/manual/"] },
    { route: "/api-index.json", jsonKey: "symbols" },
    { route: "/docs-manifest.json", jsonKey: "build" },
    { route: "/site.webmanifest", tokens: ["\"short_name\": \"Start\"", "\"short_name\": \"API\"", "/start.html"] },
    { route: "/sw.js", tokens: ["networkFirst(request, true)"] },
    { route: "/sitemap.xml", tokens: ["/start.html", "/learn.html", "/wiki/site/en/api/index.html"] },
];

function record(id, target, status, started, detail = null) {
    healthResults.push({
        id,
        kind: "route",
        target,
        status,
        duration_ms: Math.max(0, Math.round(performance.now() - started)),
        detail,
    });
}

function isRetryableStatus(status) {
    return status === 408 || status === 425 || status === 429 || status >= 500;
}

function wait(milliseconds) {
    return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function fetchText(target) {
    let lastError = null;

    for (let attempt = 1; attempt <= requestAttempts; attempt += 1) {
        try {
            const response = await fetch(target, {
                headers: { "user-agent": "Infernux-website-health/1.0" },
                signal: AbortSignal.timeout(requestTimeoutMs),
            });
            if (!response.ok) {
                const error = new Error(`HTTP ${response.status}`);
                error.retryable = isRetryableStatus(response.status);
                throw error;
            }

            return { response, body: await response.text(), attempt };
        } catch (error) {
            lastError = error;
            const retryable = error.retryable !== false;
            if (!retryable || attempt === requestAttempts) break;

            console.warn(`RETRY ${target.pathname}: ${error.message} (attempt ${attempt}/${requestAttempts})`);
            await wait(attempt * 1_000);
        }
    }

    throw lastError;
}

for (const check of checks) {
    const target = new URL(check.route, base);
    const started = performance.now();
    try {
        const { response, body, attempt } = await fetchText(target);
        for (const token of check.tokens || []) {
            if (!body.includes(token)) throw new Error(`missing '${token}'`);
        }
        for (const token of check.forbid || []) {
            if (body.includes(token)) throw new Error(`contains obsolete '${token}'`);
        }
        if (check.jsonKey) {
            const data = JSON.parse(body);
            if (!(check.jsonKey in data)) throw new Error(`JSON is missing '${check.jsonKey}'`);
            if (check.route === "/docs-manifest.json") deployedManifest = data;
        }
        const attemptDetail = attempt > 1 ? ` after ${attempt} attempts` : "";
        console.log(`PASS ${check.route}${attemptDetail}`);
        record(check.route, target.toString(), "passed", started, `HTTP ${response.status}${attemptDetail}`);
    } catch (error) {
        failures.push(`${check.route}: ${error.message}`);
        console.error(`FAIL ${check.route}: ${error.message}`);
        record(check.route, target.toString(), "failed", started, error.message);
    }
}

if (deployedManifest && !allowUnstamped && deployedManifest.build?.status !== "stamped") {
    failures.push("docs-manifest.json: production documentation build is not stamped");
}

const finishedAt = new Date();
const repository = process.env.GITHUB_REPOSITORY || "ChenlizheMe/Infernux";
const serverUrl = process.env.GITHUB_SERVER_URL || "https://github.com";
const runUrl = process.env.GITHUB_RUN_ID ? `${serverUrl}/${repository}/actions/runs/${process.env.GITHUB_RUN_ID}` : null;
const report = buildWebsiteHealthReport({
    checkedAt: process.env.WEBSITE_HEALTH_CHECKED_AT || finishedAt.toISOString(),
    baseUrl: base,
    startedAt,
    finishedAt,
    checks: healthResults,
    manifest: deployedManifest,
    pagesBuild: null,
    environment: {
        repository,
        checkoutCommit: process.env.GITHUB_SHA,
        workflow: process.env.GITHUB_WORKFLOW,
        runId: process.env.GITHUB_RUN_ID,
        runAttempt: process.env.GITHUB_RUN_ATTEMPT,
        runUrl,
    },
});

if (reportPath) {
    await mkdir(path.dirname(reportPath), { recursive: true });
    await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
}
if (process.env.GITHUB_STEP_SUMMARY) {
    await appendFile(process.env.GITHUB_STEP_SUMMARY, renderWebsiteHealthSummary(report), "utf8");
}
if (failures.length) {
    console.error(`Deployed website health failed with ${failures.length} issue(s).`);
    process.exit(1);
}

console.log(`Deployed website health passed for ${base.origin}.`);
