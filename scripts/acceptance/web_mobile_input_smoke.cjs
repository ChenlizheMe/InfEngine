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
  if (!url) throw new Error("usage: node web_mobile_input_smoke.cjs <url> [--require-active-audio]");
  const requireActiveAudio = process.argv.includes("--require-active-audio");
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
  const consoleErrors = [];
  page.on("pageerror", (error) => pageErrors.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  try {
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 180000 });
    await page.waitForFunction(
      () => document.querySelector("#canvas")?.dataset.infernuxState === "awaiting-user-activation",
      null,
      { timeout: 240000 },
    );
    const canvasBox = await page.locator("#canvas").boundingBox();
    if (!canvasBox) throw new Error("Web Player canvas has no interactive bounds");
    await page.touchscreen.tap(
      canvasBox.x + canvasBox.width * 0.5,
      canvasBox.y + canvasBox.height * 0.5,
    );
    await page.waitForFunction(
      () => document.querySelector("#canvas")?.dataset.infernuxState === "ready",
      null,
      { timeout: 30000 },
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
      const activeVoiceMarker = diagnostics.find(
        (item) => item.includes("INFERNUX_WEB_AUDIO_ACTIVE_VOICES"),
      ) || "";
      return {
        state: canvas.dataset.infernuxState,
        pythonReady: diagnostics.some((item) => item.includes("INFERNUX_WEB_PYTHON_READY")),
        nativeModuleReady: diagnostics.some(
          (item) => item.includes("INFERNUX_WEB_NATIVE_MODULE_READY"),
        ),
        sceneReady: diagnostics.some((item) => item.includes("INFERNUX_WEB_SCENE_READY")),
        sceneRenderReady: diagnostics.some(
          (item) => item.includes("INFERNUX_WEB_SCENE_RENDER_READY"),
        ),
        firstFrameReady: diagnostics.some(
          (item) => item.includes("INFERNUX_WEB_FIRST_FRAME_READY"),
        ),
        runtimeActive: diagnostics.some(
          (item) => item.includes("INFERNUX_WEB_RUNTIME_ACTIVE"),
        ),
        pointerBridge: diagnostics.includes("INFERNUX_WEB_POINTER_BRIDGE_READY"),
        textBridge: diagnostics.includes("INFERNUX_WEB_TEXT_BRIDGE_READY"),
        visualViewport: diagnostics.includes("INFERNUX_WEB_VISUAL_VIEWPORT_READY"),
        audioReady: diagnostics.some((item) => item.includes("INFERNUX_WEB_AUDIO_READY")),
        audioContextRunning: Module.SDL3?.audioContext?.state === "running",
        activeAudioVoices: Number(activeVoiceMarker.match(/count=(\d+)/)?.[1] || 0),
        pointerDown: diagnostics.some((item) => item.includes("kind=pointer_down")),
        pointerCancel: diagnostics.some((item) => item.includes("kind=pointer_cancel")),
        textInput: diagnostics.some((item) => item.includes("kind=text_input")),
        pageHide: diagnostics.some((item) => item.includes("kind=page_hide")),
        pageShow: diagnostics.some((item) => item.includes("kind=page_show")),
        unhandledErrors: diagnostics.filter((item) => item.startsWith("ERROR:")),
        diagnosticTail: diagnostics.slice(-80),
      };
    });
    if (pageErrors.length || consoleErrors.length || result.unhandledErrors.length ||
        !result.pythonReady || !result.nativeModuleReady || !result.sceneReady ||
        !result.sceneRenderReady || !result.firstFrameReady || !result.runtimeActive ||
        !result.pointerBridge || !result.textBridge || !result.visualViewport ||
        !result.audioReady || !result.audioContextRunning ||
        (requireActiveAudio && result.activeAudioVoices < 1) ||
        !result.pointerDown || !result.pointerCancel || !result.textInput ||
        !result.pageHide || !result.pageShow) {
      throw new Error(JSON.stringify({ result, pageErrors, consoleErrors }));
    }
    delete result.diagnosticTail;
    process.stdout.write(`${JSON.stringify(result)}\n`);
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
