import assert from "node:assert/strict";
import { readFile, stat } from "node:fs/promises";
import path from "node:path";

const root = path.resolve(".");
const docsRoot = path.join(root, "docs");
const communityUrl = "https://infernux-engine.discourse.group/";
const learningCourses = JSON.parse(
    await readFile(path.join(docsRoot, "learn", "learning-courses.json"), "utf8")
);
const learningChapters = (await Promise.all(learningCourses.map(async (course) => JSON.parse(
    await readFile(path.join(docsRoot, "learn", course.manifest), "utf8")
)))).flat();

async function exists(target) {
    return stat(target).then(() => true).catch(() => false);
}

const redirect = await readFile(path.join(docsRoot, "community.html"), "utf8");
assert.ok(redirect.includes(`http-equiv="refresh" content="0; url=${communityUrl}"`), "legacy community route must redirect immediately");
assert.ok(redirect.includes(`rel="canonical" href="${communityUrl}"`), "legacy community route must canonicalize to Discourse");
assert.ok(redirect.includes(`href="${communityUrl}"`), "legacy community route must retain a visible fallback link");

for (const relative of [
    "index.html",
    "start.html",
    "learn.html",
    "roadmap.html",
    "download.html",
    "404.html",
    ...learningCourses.map((course) => path.join("learn", `${course.slug}.html`)),
    ...learningChapters.map((chapter) => path.join("learn", `${chapter.slug}.html`)),
    path.join("wiki", "theme", "main.html"),
]) {
    const source = await readFile(path.join(docsRoot, relative), "utf8");
    assert.ok(source.includes(communityUrl), `${relative} must link directly to the Discourse community`);
    assert.doesNotMatch(source, /href=["'](?:\.\.\/|\/)?community\.html(?:[?#][^"']*)?["']/i, `${relative} must not route normal navigation through the legacy redirect`);
}

const hub = await readFile(path.join(root, "packaging", "view", "discussion_view.py"), "utf8");
assert.ok(hub.includes(`FORUM_URL = "${communityUrl}"`), "InfernuxHub must open the Discourse community directly");

const issueConfig = await readFile(path.join(root, ".github", "ISSUE_TEMPLATE", "config.yml"), "utf8");
assert.ok(issueConfig.includes(communityUrl), "GitHub issue guidance must point users to the Discourse community");
assert.doesNotMatch(issueConfig, /GitHub Discussions|\/discussions\b/i, "GitHub issue guidance must not advertise the retired Discussions forum");

for (const obsolete of [
    path.join(root, "services", "community-gateway"),
    path.join(root, ".github", "DISCUSSION_TEMPLATE"),
    path.join(docsRoot, "community-topic.html"),
    path.join(docsRoot, "css", "community.css"),
    path.join(docsRoot, "js", "community-api-v5.js"),
    path.join(docsRoot, "js", "community-giscus.js"),
    path.join(docsRoot, "js", "community-topic.js"),
    path.join(docsRoot, "js", "community.js"),
    path.join(docsRoot, "tools", "check-community-readiness.mjs"),
    path.join(docsRoot, "tools", "community-readiness-core.mjs"),
    path.join(docsRoot, "tools", "test-community-client.mjs"),
    path.join(docsRoot, "tools", "test-community-readiness.mjs"),
]) {
    assert.equal(await exists(obsolete), false, `${path.relative(root, obsolete)} is part of the retired GitHub forum suite`);
}

console.log("Community link test passed: website and Hub open Discourse directly, with only a legacy redirect retained.");
