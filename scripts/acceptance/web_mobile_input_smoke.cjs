"use strict";

const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");
const { PNG } = require("pngjs");

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

async function activateCanvas(page, canvasBox, cdpEndpoint) {
  const x = canvasBox.x + canvasBox.width * 0.5;
  const y = canvasBox.y + canvasBox.height * 0.5;
  if (!cdpEndpoint) {
    await page.touchscreen.tap(x, y);
    return;
  }

  const session = await page.context().newCDPSession(page);
  try {
    await session.send("Input.dispatchTouchEvent", {
      type: "touchStart",
      touchPoints: [{ x, y, id: 1, radiusX: 12, radiusY: 9, force: 0.65 }],
    });
    await session.send("Input.dispatchTouchEvent", {
      type: "touchEnd",
      touchPoints: [],
    });
  } finally {
    await session.detach();
  }
}

async function measureCanvasFrame(canvas) {
  const image = PNG.sync.read(await canvas.screenshot({ animations: "disabled" }));
  const stride = Math.max(1, Math.ceil(Math.sqrt(image.width * image.height / 65536)));
  let count = 0;
  let luminanceSum = 0;
  let luminanceSquareSum = 0;
  let nonBlack = 0;
  let opaque = 0;
  let upperLuminance = 0;
  let lowerLuminance = 0;
  let upperCount = 0;
  let lowerCount = 0;
  const quantizedColors = new Set();
  for (let y = 0; y < image.height; y += stride) {
    for (let x = 0; x < image.width; x += stride) {
      const index = (y * image.width + x) * 4;
      const red = image.data[index] / 255;
      const green = image.data[index + 1] / 255;
      const blue = image.data[index + 2] / 255;
      const alpha = image.data[index + 3] / 255;
      const luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue;
      count += 1;
      luminanceSum += luminance;
      luminanceSquareSum += luminance * luminance;
      if (luminance > 2 / 255) nonBlack += 1;
      if (alpha > 0.99) opaque += 1;
      if (y < image.height / 3) {
        upperLuminance += luminance;
        upperCount += 1;
      }
      if (y >= image.height * 2 / 3) {
        lowerLuminance += luminance;
        lowerCount += 1;
      }
      quantizedColors.add(
        `${image.data[index] >> 4}:${image.data[index + 1] >> 4}:${image.data[index + 2] >> 4}`,
      );
    }
  }
  const mean = luminanceSum / count;
  return {
    width: image.width,
    height: image.height,
    meanLuminance: mean,
    luminanceDeviation: Math.sqrt(Math.max(0, luminanceSquareSum / count - mean * mean)),
    nonBlackRatio: nonBlack / count,
    opaqueRatio: opaque / count,
    upperMeanLuminance: upperLuminance / Math.max(1, upperCount),
    lowerMeanLuminance: lowerLuminance / Math.max(1, lowerCount),
    quantizedColorCount: quantizedColors.size,
  };
}

async function main() {
  const url = process.argv[2];
  if (!url) {
    throw new Error(
      "usage: node web_mobile_input_smoke.cjs <url> " +
      "[--cdp-endpoint URL] [--require-active-audio] [--startup-timeout-ms N]",
    );
  }
  const requireActiveAudio = process.argv.includes("--require-active-audio");
  const cdpIndex = process.argv.indexOf("--cdp-endpoint");
  const cdpEndpoint = cdpIndex >= 0 ? process.argv[cdpIndex + 1] : "";
  if (cdpIndex >= 0 && !cdpEndpoint) {
    throw new Error("--cdp-endpoint requires a URL");
  }
  const timeoutIndex = process.argv.indexOf("--startup-timeout-ms");
  const startupTimeout = timeoutIndex >= 0
    ? Number(process.argv[timeoutIndex + 1])
    : 240000;
  if (!Number.isFinite(startupTimeout) || startupTimeout <= 0) {
    throw new Error("--startup-timeout-ms must be a positive number");
  }
  let browser;
  let page;
  if (cdpEndpoint) {
    browser = await chromium.connectOverCDP(cdpEndpoint, { timeout: 30000 });
    const contexts = browser.contexts();
    if (!contexts.length) {
      throw new Error("the CDP browser did not expose a browser context");
    }
    const pages = contexts.flatMap((context) => context.pages());
    page = pages.find((candidate) => candidate.url() === url) ||
      pages.find((candidate) => candidate.url().includes("infernux-player")) ||
      pages[0] ||
      await contexts[0].newPage();
  } else {
    const executablePath = resolveBrowserExecutable();
    browser = await chromium.launch({
      executablePath,
      headless: true,
      timeout: 30000,
      args: ["--enable-unsafe-webgpu", "--disable-gpu-sandbox"],
    });
    page = await browser.newPage({
      viewport: { width: 412, height: 915 },
      deviceScaleFactor: 2,
      hasTouch: true,
      isMobile: true,
    });
  }
  const pageErrors = [];
  const consoleErrors = [];
  const consoleMessages = [];
  page.on("pageerror", (error) => pageErrors.push(String(error)));
  page.on("console", (message) => {
    consoleMessages.push(`${message.type()}: ${message.text()}`);
    if (consoleMessages.length > 200) consoleMessages.shift();
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  try {
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 180000 });
    try {
      await page.waitForFunction(
        () => ["awaiting-user-activation", "ready"].includes(
          document.querySelector("#canvas")?.dataset.infernuxState,
        ),
        null,
        { timeout: startupTimeout },
      );
    } catch (error) {
      const startup = await page.evaluate(() => {
        const canvas = document.querySelector("#canvas");
        let diagnostics = [];
        try {
          diagnostics = JSON.parse(canvas?.dataset.infernuxDiagnostics || "[]");
        } catch (parseError) {
          diagnostics = [`diagnostic parse failed: ${parseError}`];
        }
        return {
          documentReadyState: document.readyState,
          canvasFound: Boolean(canvas),
          canvasState: canvas?.dataset.infernuxState || "",
          diagnosticTail: diagnostics.slice(-120),
          moduleCalledRun: globalThis.Module?.calledRun ?? null,
          moduleRuntimeInitialized: globalThis.Module?.runtimeInitialized ?? null,
        };
      });
      throw new Error(JSON.stringify({
        phase: "startup",
        startup,
        pageErrors,
        consoleErrors,
        consoleMessages,
        cause: String(error),
      }));
    }
    const canvas = page.locator("#canvas");
    const canvasBox = await canvas.boundingBox();
    if (!canvasBox) throw new Error("Web Player canvas has no interactive bounds");
    const stateBeforeActivation = await canvas.getAttribute("data-infernux-state");
    await page.waitForTimeout(250);
    const frameBeforeActivation = await measureCanvasFrame(canvas);
    if (stateBeforeActivation !== "ready") {
      await activateCanvas(page, canvasBox, cdpEndpoint);
    }
    await page.waitForFunction(
      () => document.querySelector("#canvas")?.dataset.infernuxState === "ready",
      null,
      { timeout: 30000 },
    );
    await page.waitForTimeout(250);
    const frameAfterActivation = await measureCanvasFrame(canvas);
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
    const frameAfterInput = await measureCanvasFrame(canvas);
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
    result.frameBeforeActivation = frameBeforeActivation;
    result.frameAfterActivation = frameAfterActivation;
    result.frameAfterInput = frameAfterInput;
    const frameIsVisible = (frame) => (
      frame.nonBlackRatio >= 0.1 &&
      frame.luminanceDeviation >= 0.01 &&
      frame.quantizedColorCount >= 8
    );
    const inputPreservedFrame = (
      frameAfterInput.meanLuminance >= Math.max(
        0.01,
        frameAfterActivation.meanLuminance * 0.05,
      )
    );
    if (pageErrors.length || consoleErrors.length || result.unhandledErrors.length ||
        !result.pythonReady || !result.nativeModuleReady || !result.sceneReady ||
        !result.sceneRenderReady || !result.firstFrameReady || !result.runtimeActive ||
        !result.pointerBridge || !result.textBridge || !result.visualViewport ||
        !result.audioReady || !result.audioContextRunning ||
        (requireActiveAudio && result.activeAudioVoices < 1) ||
        !result.pointerDown || !result.pointerCancel || !result.textInput ||
        !result.pageHide || !result.pageShow ||
        !frameIsVisible(frameAfterActivation) || !frameIsVisible(frameAfterInput) ||
        !inputPreservedFrame) {
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
