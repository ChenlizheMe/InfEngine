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

async function readCanvasFrame(canvas) {
  return PNG.sync.read(await canvas.screenshot({ animations: "disabled" }));
}

function summarizeCanvasFrame(image) {
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

async function measureCanvasFrame(canvas) {
  return summarizeCanvasFrame(await readCanvasFrame(canvas));
}

function compareCanvasFrames(reference, candidate) {
  if (reference.width !== candidate.width || reference.height !== candidate.height) {
    throw new Error("Web Player diagnostic frames changed dimensions");
  }
  const stride = Math.max(
    1,
    Math.ceil(Math.sqrt(reference.width * reference.height / 65536)),
  );
  let samples = 0;
  let changed = 0;
  let absoluteDifference = 0;
  for (let y = 0; y < reference.height; y += stride) {
    for (let x = 0; x < reference.width; x += stride) {
      const index = (y * reference.width + x) * 4;
      const difference = (
        Math.abs(reference.data[index] - candidate.data[index]) +
        Math.abs(reference.data[index + 1] - candidate.data[index + 1]) +
        Math.abs(reference.data[index + 2] - candidate.data[index + 2])
      ) / (3 * 255);
      samples += 1;
      absoluteDifference += difference;
      if (difference > 2 / 255) changed += 1;
    }
  }
  return {
    meanAbsoluteDifference: absoluteDifference / samples,
    changedPixelRatio: changed / samples,
  };
}

async function setRenderDiagnostic(page, feature, enabled) {
  await page.evaluate(({ feature, enabled }) => {
    Module.ccall(
      "InfernuxWebSetRenderDiagnostic",
      null,
      ["number", "number"],
      [feature, enabled ? 1 : 0],
    );
  }, { feature, enabled });
  await page.waitForTimeout(120);
}

async function main() {
  const url = process.argv[2];
  if (!url) {
    throw new Error(
      "usage: node web_mobile_input_smoke.cjs <url> " +
      "[--cdp-endpoint URL] [--require-active-audio] [--startup-timeout-ms N] " +
      "[--viewport-width N] [--viewport-height N] " +
      "[--expect-presentation fullscreen-borderless|windowed] " +
      "[--expect-render-width N] [--expect-render-height N]",
    );
  }
  const argumentValue = (name, fallback = "") => {
    const index = process.argv.indexOf(name);
    return index >= 0 ? process.argv[index + 1] : fallback;
  };
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
  const viewportWidth = Number(argumentValue("--viewport-width", "412"));
  const viewportHeight = Number(argumentValue("--viewport-height", "915"));
  const expectedPresentation = argumentValue("--expect-presentation");
  const expectedRenderWidth = Number(argumentValue("--expect-render-width", "0"));
  const expectedRenderHeight = Number(argumentValue("--expect-render-height", "0"));
  if (!Number.isInteger(viewportWidth) || viewportWidth <= 0 ||
      !Number.isInteger(viewportHeight) || viewportHeight <= 0) {
    throw new Error("viewport dimensions must be positive integers");
  }
  if (expectedPresentation &&
      !["fullscreen-borderless", "windowed"].includes(expectedPresentation)) {
    throw new Error("--expect-presentation must be fullscreen-borderless or windowed");
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
      viewport: { width: viewportWidth, height: viewportHeight },
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
        () => document.querySelector("#canvas")?.dataset.infernuxState === "ready",
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
    const initialKeyboardFocus = await page.evaluate(
      () => document.activeElement === document.querySelector("#canvas"),
    );
    await page.keyboard.down("w");
    await page.waitForTimeout(120);
    const nativeWPressed = await page.evaluate(() => Module.ccall(
      "InfernuxWebGetKeyState", "number", ["number"], [26],
    ) === 1);
    await page.keyboard.up("w");
    await page.waitForTimeout(120);
    const nativeWReleased = await page.evaluate(() => Module.ccall(
      "InfernuxWebGetKeyState", "number", ["number"], [26],
    ) === 0);
    await page.waitForTimeout(250);
    const frameBeforeActivation = await measureCanvasFrame(canvas);
    await page.evaluate(() => {
      const loader = document.querySelector("#infernux-loader");
      if (loader) loader.style.visibility = "hidden";
    });
    await page.waitForTimeout(120);
    const featureBaseline = await readCanvasFrame(canvas);
    await setRenderDiagnostic(page, 1, false);
    const shadowsDisabled = await readCanvasFrame(canvas);
    await setRenderDiagnostic(page, 1, true);
    await setRenderDiagnostic(page, 0, false);
    const skyDisabled = await readCanvasFrame(canvas);
    await setRenderDiagnostic(page, 0, true);
    const sceneFrame = summarizeCanvasFrame(featureBaseline);
    const shadowDifference = compareCanvasFrames(featureBaseline, shadowsDisabled);
    const skyDifference = compareCanvasFrames(featureBaseline, skyDisabled);
    await page.evaluate(() => {
      const loader = document.querySelector("#infernux-loader");
      if (loader) loader.style.visibility = "";
    });
    await activateCanvas(page, canvasBox, cdpEndpoint);
    await page.waitForFunction(() => {
      const diagnostics = JSON.parse(
        document.querySelector("#canvas")?.dataset.infernuxDiagnostics || "[]",
      );
      return diagnostics.some((item) => item.includes("INFERNUX_WEB_AUDIO_READY"));
    }, null, { timeout: 30000 });
    await page.waitForTimeout(250);
    const frameAfterActivation = await measureCanvasFrame(canvas);
    const contextMenuPrevented = await page.evaluate(() => {
      const canvas = document.querySelector("#canvas");
      const contextMenu = new MouseEvent("contextmenu", {
        button: 2,
        buttons: 2,
        clientX: 12,
        clientY: 12,
        bubbles: true,
        cancelable: true,
      });
      canvas.dispatchEvent(contextMenu);
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
      return contextMenu.defaultPrevented;
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
        skyReady: diagnostics.some(
          (item) => item.includes("INFERNUX_WEB_SKY_READY"),
        ),
        shadowReady: diagnostics.some(
          (item) => item.includes("INFERNUX_WEB_SHADOW_READY"),
        ),
        screenUiReady: diagnostics.some(
          (item) => item.includes("INFERNUX_WEB_SCREEN_UI_READY"),
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
        keyboardFocusBridge: diagnostics.includes("INFERNUX_WEB_KEYBOARD_FOCUS_READY"),
        audioReady: diagnostics.some((item) => item.includes("INFERNUX_WEB_AUDIO_READY")),
        audioContextRunning: Module.SDL3?.audioContext?.state === "running",
        activeAudioVoices: Number(activeVoiceMarker.match(/count=(\d+)/)?.[1] || 0),
        pointerDown: diagnostics.some((item) => item.includes("kind=pointer_down")),
        pointerCancel: diagnostics.some((item) => item.includes("kind=pointer_cancel")),
        textInput: diagnostics.some((item) => item.includes("kind=text_input")),
        pageHide: diagnostics.some((item) => item.includes("kind=page_hide")),
        pageShow: diagnostics.some((item) => item.includes("kind=page_show")),
        presentation: document.body.dataset.infernuxPresentation || "",
        canvasLayout: (() => {
          const rect = canvas.getBoundingClientRect();
          return {
            left: rect.left,
            top: rect.top,
            cssWidth: rect.width,
            cssHeight: rect.height,
            renderWidth: canvas.width,
            renderHeight: canvas.height,
            viewportWidth: window.visualViewport?.width || window.innerWidth,
            viewportHeight: window.visualViewport?.height || window.innerHeight,
          };
        })(),
        unhandledErrors: diagnostics.filter((item) => item.startsWith("ERROR:")),
        diagnosticTail: diagnostics.slice(-80),
      };
    });
    result.frameBeforeActivation = frameBeforeActivation;
    result.sceneFrame = sceneFrame;
    result.shadowDifference = shadowDifference;
    result.skyDifference = skyDifference;
    result.frameAfterActivation = frameAfterActivation;
    result.frameAfterInput = frameAfterInput;
    result.contextMenuPrevented = contextMenuPrevented;
    result.initialKeyboardFocus = initialKeyboardFocus;
    result.nativeWPressed = nativeWPressed;
    result.nativeWReleased = nativeWReleased;
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
    const skyIsVisible = (
      skyDifference.changedPixelRatio >= 0.2 &&
      skyDifference.meanAbsoluteDifference >= 0.03
    );
    const shadowsAreVisible = (
      shadowDifference.changedPixelRatio >= 0.003 &&
      shadowDifference.meanAbsoluteDifference >= 0.0002
    );
    const presentationMatches = !expectedPresentation ||
      result.presentation === expectedPresentation;
    const renderSizeMatches = (
      (!expectedRenderWidth || result.canvasLayout.renderWidth === expectedRenderWidth) &&
      (!expectedRenderHeight || result.canvasLayout.renderHeight === expectedRenderHeight)
    );
    const centeredWindowMatches = expectedPresentation !== "windowed" || (
      result.canvasLayout.cssWidth <= result.canvasLayout.viewportWidth + 1 &&
      result.canvasLayout.cssHeight <= result.canvasLayout.viewportHeight + 1 &&
      Math.abs(
        result.canvasLayout.left * 2 + result.canvasLayout.cssWidth -
        result.canvasLayout.viewportWidth
      ) <= 2 &&
      Math.abs(
        result.canvasLayout.top * 2 + result.canvasLayout.cssHeight -
        result.canvasLayout.viewportHeight
      ) <= 2
    );
    if (pageErrors.length || consoleErrors.length || result.unhandledErrors.length ||
        !result.pythonReady || !result.nativeModuleReady || !result.sceneReady ||
        !result.sceneRenderReady || !result.skyReady || !result.shadowReady ||
        !result.screenUiReady ||
        !result.firstFrameReady || !result.runtimeActive ||
        !result.pointerBridge || !result.textBridge || !result.visualViewport ||
        !result.keyboardFocusBridge || !result.initialKeyboardFocus ||
        !result.nativeWPressed || !result.nativeWReleased ||
        !result.audioReady || !result.audioContextRunning ||
        (requireActiveAudio && result.activeAudioVoices < 1) ||
        !result.pointerDown || !result.pointerCancel || !result.textInput ||
        !result.pageHide || !result.pageShow ||
        stateBeforeActivation !== "ready" || !result.contextMenuPrevented ||
        !presentationMatches || !renderSizeMatches || !centeredWindowMatches ||
        !frameIsVisible(frameBeforeActivation) ||
        !frameIsVisible(frameAfterActivation) || !frameIsVisible(frameAfterInput) ||
        !inputPreservedFrame || !skyIsVisible || !shadowsAreVisible) {
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
