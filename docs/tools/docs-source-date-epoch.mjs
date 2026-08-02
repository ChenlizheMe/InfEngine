import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const manifestPath = path.resolve(scriptDir, "..", "docs-manifest.json");
const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
const generatedAt = manifest?.build?.generated_at;
const timestamp = Date.parse(generatedAt || "");

if (manifest?.build?.status !== "stamped" || !Number.isFinite(timestamp)) {
    throw new Error("docs-manifest.json must contain stamped build.generated_at provenance");
}

process.stdout.write(`${Math.floor(timestamp / 1000)}\n`);
