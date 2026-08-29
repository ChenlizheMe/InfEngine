"use strict";

const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

function resolveBrowserExecutable() {
  if (process.env.INFERNUX_WEB_BROWSER) {
    return process.env.INFERNUX_WEB_BROWSER;
  }
  if (process.platform !== "win32") {
    return undefined;
  }

  const roots = [
    process.env["ProgramFiles(x86)"],
    process.env.ProgramFiles,
    process.env.LOCALAPPDATA,
  ].filter(Boolean);
  const candidates = roots.map((root) => path.join(
    root,
    "Microsoft",
    "Edge",
    "Application",
    "msedge.exe",
  ));
  return candidates.find((candidate) => fs.existsSync(candidate));
}

async function main() {
  const url = process.argv[2];
  if (!url) throw new Error("usage: node web_mobile_input_smoke.cjs <url>");
  const executablePath = resolveBrowserExecutable();
  const browser = await chromium.launch({
    executablePath,
    headless: true,
    timeout: 30000,
    args: ["--enable-unsafe-webgpu", "--disable-gpu-sandbox"],
  });
  const page = await browser.newPage({
    viewport: { width: 412, height: 915 },
    deviceScaleFactor: 2,
    hasTouch: true,
    isMobile: true,
  });
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(String(error)));
  try {
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 180000 });
    await page.waitForFunction(
      () => document.querySelector("#canvas")?.dataset.infernuxState === "ready",
      null,
      { timeout: 240000 },
    );
    await page.evaluate(() => {
      const canvas = document.querySelector("#canvas");
      const pointer = (type, pointerId, x, y, primary) => {
        canvas.dispatchEvent(new PointerEvent(type, {
          pointerId,
          pointerType: "touch",
          isPrimary: primary,
          clientX: x,
          clientY: y,
          width: 24,
          height: 18,
          pressure: type === "pointerup" || type === "pointercancel" ? 0 : 0.65,
          bubbles: true,
          cancelable: true,
        }));
      };
      pointer("pointerdown", 41, 90, 600, true);
      pointer("pointerdown", 77, 320, 600, false);
      pointer("pointermove", 41, 115, 575, true);
      pointer("pointercancel", 77, 320, 600, false);
      pointer("pointerup", 41, 115, 575, true);

      Module.infernuxBeginTextInput("", "text");
      const input = document.querySelector("#infernux-text-input");
      input.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }));
      input.value = "中文🙂";
      input.dispatchEvent(new CompositionEvent("compositionend", {
        data: "中文🙂",
        bubbles: true,
      }));
      input.dispatchEvent(new InputEvent("input", {
        data: "中文🙂",
        inputType: "insertCompositionText",
        bubbles: true,
      }));
      Module.infernuxEndTextInput();
      Module.ccall("InfernuxWebPageLifecycle", null, ["number"], [0]);
      Module.ccall("InfernuxWebPageLifecycle", null, ["number"], [1]);
    });
    await page.waitForTimeout(1000);
    const result = await page.evaluate(() => {
      const canvas = document.querySelector("#canvas");
      const diagnostics = JSON.parse(canvas.dataset.infernuxDiagnostics || "[]");
      return {
        state: canvas.dataset.infernuxState,
        pointerBridge: diagnostics.includes("INFERNUX_WEB_POINTER_BRIDGE_READY"),
        textBridge: diagnostics.includes("INFERNUX_WEB_TEXT_BRIDGE_READY"),
        visualViewport: diagnostics.includes("INFERNUX_WEB_VISUAL_VIEWPORT_READY"),
        pointerDown: diagnostics.some((item) => item.includes("kind=pointer_down")),
        pointerCancel: diagnostics.some((item) => item.includes("kind=pointer_cancel")),
        textInput: diagnostics.some((item) => item.includes("kind=text_input")),
        pageHide: diagnostics.some((item) => item.includes("kind=page_hide")),
        pageShow: diagnostics.some((item) => item.includes("kind=page_show")),
        unhandledErrors: diagnostics.filter((item) => item.startsWith("ERROR:")),
      };
    });
    if (pageErrors.length || result.unhandledErrors.length ||
        !result.pointerBridge || !result.textBridge || !result.visualViewport ||
        !result.pointerDown || !result.pointerCancel || !result.textInput ||
        !result.pageHide || !result.pageShow) {
      throw new Error(JSON.stringify({ result, pageErrors }));
    }
    process.stdout.write(`${JSON.stringify(result)}\n`);
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
