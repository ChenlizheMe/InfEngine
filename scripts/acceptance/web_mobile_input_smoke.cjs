"use strict";

const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");
const { PNG } = require("pngjs");

function writeJsonAtomic(outputPath, payload) {
  if (!outputPath) return;
  const resolved = path.resolve(outputPath);
  fs.mkdirSync(path.dirname(resolved), { recursive: true });
  const temporary = `${resolved}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  fs.renameSync(temporary, resolved);
}

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
  const startedAt = process.hrtime.bigint();
  let url = process.argv[2];
  if (!url) {
    throw new Error(
      "usage: node web_mobile_input_smoke.cjs <url> " +
      "[--report PATH] [--cdp-endpoint URL] [--require-active-audio] " +
      "[--startup-timeout-ms N] " +
      "[--viewport-width N] [--viewport-height N] " +
      "[--expect-presentation fullscreen-borderless|windowed] " +
      "[--expect-render-width N] [--expect-render-height N] " +
      "[--track-object NAME] [--movement-key KEY|--movement-touch] " +
      "[--min-displacement N] [--require-diagnostic TEXT] " +
      "[--require-diagnostic-order BEFORE=>AFTER] " +
      "[--capture-frame-output PATH] [--skip-frame-checks] " +
      "[--capture-only --fixed-delta N --pause-after-frame N] " +
      "[--device-scale-factor N] " +
      "[--verify-particle-bloom] [--verify-native-multitouch] " +
      "[--verify-mobile-ime]",
    );
  }
  const argumentValue = (name, fallback = "") => {
    const index = process.argv.indexOf(name);
    return index >= 0 ? process.argv[index + 1] : fallback;
  };
  const argumentValues = (name) => {
    const values = [];
    for (let index = 0; index < process.argv.length; index += 1) {
      if (process.argv[index] !== name) continue;
      const value = process.argv[index + 1];
      if (!value || value.startsWith("--")) {
        throw new Error(`${name} requires a value`);
      }
      values.push(value);
    }
    return values;
  };
  const requireActiveAudio = process.argv.includes("--require-active-audio");
  const reportPath = argumentValue("--report");
  const movementTouch = process.argv.includes("--movement-touch");
  const skipFrameChecks = process.argv.includes("--skip-frame-checks");
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
  const trackedObject = argumentValue("--track-object");
  const movementKey = argumentValue("--movement-key", "w");
  const minimumDisplacement = Number(argumentValue("--min-displacement", "0.02"));
  const requiredDiagnostics = argumentValues("--require-diagnostic");
  const forbiddenDiagnostics = [
    "AudioSource::StartVoice: AudioEngine not initialized",
  ];
  const requiredDiagnosticOrders = argumentValues("--require-diagnostic-order").map(
    (value) => {
      const separator = value.indexOf("=>");
      if (separator <= 0 || separator >= value.length - 2) {
        throw new Error(
          "--require-diagnostic-order must use the form BEFORE=>AFTER",
        );
      }
      return {
        before: value.slice(0, separator),
        after: value.slice(separator + 2),
      };
    },
  );
  const captureFrameOutput = argumentValue("--capture-frame-output");
  const captureOnly = process.argv.includes("--capture-only");
  const verifyParticleBloom = process.argv.includes("--verify-particle-bloom");
  const verifyNativeMultitouch = process.argv.includes("--verify-native-multitouch");
  const verifyMobileIme = process.argv.includes("--verify-mobile-ime");
  if (verifyMobileIme && !cdpEndpoint) {
    throw new Error("--verify-mobile-ime requires a physical browser through --cdp-endpoint");
  }
  const fixedDelta = Number(argumentValue("--fixed-delta", "0"));
  const pauseAfterFrame = Number(argumentValue("--pause-after-frame", "0"));
  const deterministicCapture = fixedDelta !== 0 || pauseAfterFrame !== 0;
  const deviceScaleFactor = Number(argumentValue(
    "--device-scale-factor",
    deterministicCapture ? "1" : "2",
  ));
  if (!Number.isInteger(viewportWidth) || viewportWidth <= 0 ||
      !Number.isInteger(viewportHeight) || viewportHeight <= 0) {
    throw new Error("viewport dimensions must be positive integers");
  }
  if (!Number.isFinite(deviceScaleFactor) || deviceScaleFactor <= 0) {
    throw new Error("--device-scale-factor must be a positive number");
  }
  for (const [name, value] of [
    ["--expect-render-width", expectedRenderWidth],
    ["--expect-render-height", expectedRenderHeight],
  ]) {
    if (!Number.isSafeInteger(value) || value < 0) {
      throw new Error(`${name} must be a non-negative integer`);
    }
  }
  if (expectedPresentation &&
      !["fullscreen-borderless", "windowed"].includes(expectedPresentation)) {
    throw new Error("--expect-presentation must be fullscreen-borderless or windowed");
  }
  if (!Number.isFinite(minimumDisplacement) || minimumDisplacement < 0) {
    throw new Error("--min-displacement must be a non-negative number");
  }
  if (deterministicCapture) {
    if (!Number.isFinite(fixedDelta) || fixedDelta <= 0 || fixedDelta > 0.25 ||
        !Number.isSafeInteger(pauseAfterFrame) || pauseAfterFrame <= 0) {
      throw new Error(
        "deterministic capture requires --fixed-delta in (0, 0.25] and " +
        "a positive integer --pause-after-frame",
      );
    }
    if (!captureOnly || !captureFrameOutput) {
      throw new Error(
        "deterministic capture requires --capture-only and --capture-frame-output",
      );
    }
    if (!expectedRenderWidth || !expectedRenderHeight) {
      throw new Error(
        "deterministic capture requires explicit --expect-render-width and " +
        "--expect-render-height",
      );
    }
    if (viewportWidth !== expectedRenderWidth ||
        viewportHeight !== expectedRenderHeight) {
      throw new Error(
        "deterministic capture viewport must exactly match the expected render size",
      );
    }
    if (deviceScaleFactor !== 1) {
      throw new Error("deterministic capture requires --device-scale-factor 1");
    }
    const target = new URL(url);
    target.searchParams.set("acceptanceFixedDelta", String(fixedDelta));
    target.searchParams.set("acceptancePauseFrame", String(pauseAfterFrame));
    url = target.toString();
  } else if (captureOnly) {
    throw new Error("--capture-only requires the deterministic clock arguments");
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
      deviceScaleFactor,
      hasTouch: true,
      isMobile: true,
    });
  }
  const pageErrors = [];
  const consoleErrors = [];
  const consoleMessages = [];
  page.on("pageerror", (error) => pageErrors.push(error?.stack || String(error)));
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
    const movementTouchGeometry = movementTouch ? await page.evaluate(() => {
      const canvas = document.querySelector("#canvas");
      const safeAreaProbe = document.querySelector("#infernux-safe-area-probe");
      const viewport = window.visualViewport;
      const rect = canvas.getBoundingClientRect();
      const safe = getComputedStyle(safeAreaProbe);
      const pixels = (value) => Math.max(0, Number.parseFloat(value) || 0);
      const viewportLeft = viewport.offsetLeft;
      const viewportTop = viewport.offsetTop;
      const safeLeft = Math.max(
        0,
        Math.min(rect.width, viewportLeft + pixels(safe.paddingLeft) - rect.left),
      );
      const safeBottom = Math.max(
        0,
        Math.min(
          rect.height,
          rect.bottom - (viewportTop + viewport.height - pixels(safe.paddingBottom)),
        ),
      );
      const scale = Math.min(rect.width / 1920, rect.height / 1080);
      const centerOffset = (52 + 125) * scale;
      const centerY = rect.bottom - safeBottom - centerOffset;
      return {
        centerX: rect.left + safeLeft + centerOffset,
        centerY,
        forwardY: centerY - rect.height * 0.20,
        safeLeft,
        safeBottom,
      };
    }) : null;
    if (captureOnly) {
      await page.waitForFunction((expectedFrame) => (
        Module.ccall("InfernuxWebGetAcceptancePaused", "number", [], []) === 1 &&
        Module.ccall("InfernuxWebGetAcceptanceFrame", "number", [], []) === expectedFrame
      ), pauseAfterFrame, { timeout: startupTimeout });
      const capturedRuntimeFrame = await page.evaluate(() => Module.ccall(
        "InfernuxWebGetAcceptanceFrame", "number", [], [],
      ));
      const captureLayout = await page.evaluate(() => {
        const target = document.querySelector("#canvas");
        const rect = target.getBoundingClientRect();
        return {
          left: rect.left,
          top: rect.top,
          cssWidth: rect.width,
          cssHeight: rect.height,
          renderWidth: target.width,
          renderHeight: target.height,
          viewportWidth: window.innerWidth,
          viewportHeight: window.innerHeight,
          deviceScaleFactor: window.devicePixelRatio,
        };
      });
      const exactCaptureLayout = (
        captureLayout.renderWidth === expectedRenderWidth &&
        captureLayout.renderHeight === expectedRenderHeight &&
        Math.abs(captureLayout.cssWidth - expectedRenderWidth) <= 0.01 &&
        Math.abs(captureLayout.cssHeight - expectedRenderHeight) <= 0.01 &&
        captureLayout.viewportWidth === expectedRenderWidth &&
        captureLayout.viewportHeight === expectedRenderHeight &&
        Math.abs(captureLayout.deviceScaleFactor - 1) <= 0.001
      );
      if (!exactCaptureLayout) {
        throw new Error(JSON.stringify({
          phase: "deterministic-capture-layout",
          expectedRenderWidth,
          expectedRenderHeight,
          captureLayout,
        }));
      }
      const captureFramePath = path.resolve(captureFrameOutput);
      fs.mkdirSync(path.dirname(captureFramePath), { recursive: true });
      await canvas.screenshot({ path: captureFramePath, animations: "disabled" });
      const frame = await measureCanvasFrame(canvas);
      if (frame.width !== expectedRenderWidth ||
          frame.height !== expectedRenderHeight) {
        throw new Error(JSON.stringify({
          phase: "deterministic-capture-pixels",
          expectedRenderWidth,
          expectedRenderHeight,
          frame,
        }));
      }
      let particleBloom = null;
      if (verifyParticleBloom) {
        const particleWithBloom = await readCanvasFrame(canvas);
        await setRenderDiagnostic(page, 2, false);
        const noParticleWithBloom = await readCanvasFrame(canvas);
        await setRenderDiagnostic(page, 3, false);
        const noParticleNoBloom = await readCanvasFrame(canvas);
        await setRenderDiagnostic(page, 2, true);
        const particleNoBloom = await readCanvasFrame(canvas);
        await setRenderDiagnostic(page, 3, true);
        const diagnosticBase = captureFramePath.replace(/\.png$/i, "");
        for (const [suffix, image] of [
          ["particle-with-bloom", particleWithBloom],
          ["no-particle-with-bloom", noParticleWithBloom],
          ["no-particle-no-bloom", noParticleNoBloom],
          ["particle-no-bloom", particleNoBloom],
        ]) {
          fs.writeFileSync(`${diagnosticBase}.${suffix}.png`, PNG.sync.write(image));
        }
        const withBloomContribution = compareCanvasFrames(
          particleWithBloom,
          noParticleWithBloom,
        );
        const withoutBloomContribution = compareCanvasFrames(
          particleNoBloom,
          noParticleNoBloom,
        );
        const haloPixelRatio = Math.max(
          0,
          withBloomContribution.changedPixelRatio -
            withoutBloomContribution.changedPixelRatio,
        );
        particleBloom = {
          withBloomContribution,
          withoutBloomContribution,
          haloPixelRatio,
          participates: (
            withBloomContribution.changedPixelRatio >= 0.0001 &&
            haloPixelRatio >= 0.0001
          ),
        };
      }
      const diagnosticTail = await page.evaluate(() => {
        const canvas = document.querySelector("#canvas");
        const diagnostics = JSON.parse(canvas?.dataset.infernuxDiagnostics || "[]");
        return diagnostics.slice(-120);
      });
      const requiredDiagnosticMatches = Object.fromEntries(
        requiredDiagnostics.map((requirement) => [
          requirement,
          diagnosticTail.find((item) => item.includes(requirement)) || "",
        ]),
      );
      const requiredDiagnosticOrderResults = requiredDiagnosticOrders.map(
        ({ before, after }) => {
          const beforeIndex = diagnosticTail.findIndex((item) => item.includes(before));
          const afterIndex = diagnosticTail.findIndex((item) => item.includes(after));
          return {
            before,
            after,
            beforeIndex,
            afterIndex,
            ordered: beforeIndex >= 0 && afterIndex > beforeIndex,
          };
        },
      );
      const forbiddenDiagnosticMatches = Object.fromEntries(
        forbiddenDiagnostics.map((requirement) => [
          requirement,
          diagnosticTail.find((item) => item.includes(requirement)) || "",
        ]),
      );
      const result = {
        state: await canvas.getAttribute("data-infernux-state"),
        captureOnly: true,
        fixedDeltaSeconds: fixedDelta,
        pauseAfterFrame,
        capturedRuntimeFrame,
        captureFramePath,
        captureLayout,
        frame,
        particleBloom,
        requiredDiagnostics: requiredDiagnosticMatches,
        requiredDiagnosticOrders: requiredDiagnosticOrderResults,
        forbiddenDiagnostics: forbiddenDiagnosticMatches,
        diagnosticTail,
      };
      if (capturedRuntimeFrame !== pauseAfterFrame ||
          (verifyParticleBloom && !particleBloom?.participates) ||
          Object.values(requiredDiagnosticMatches).some((match) => !match) ||
          requiredDiagnosticOrderResults.some((order) => !order.ordered) ||
          Object.values(forbiddenDiagnosticMatches).some((match) => match) ||
          pageErrors.length || consoleErrors.length) {
        throw new Error(JSON.stringify(result));
      }
      writeJsonAtomic(reportPath, {
        schema: 1,
        status: "passed",
        url,
        elapsed_seconds: Number(process.hrtime.bigint() - startedAt) / 1e9,
        result,
      });
      process.stdout.write(`${JSON.stringify(result)}\n`);
      return;
    }
    const stateBeforeActivation = await canvas.getAttribute("data-infernux-state");
    const initialKeyboardFocus = await page.evaluate(
      () => document.activeElement === document.querySelector("#canvas"),
    );
    let captureFramePath = "";
    if (captureFrameOutput) {
      captureFramePath = path.resolve(captureFrameOutput);
      fs.mkdirSync(path.dirname(captureFramePath), { recursive: true });
      await canvas.screenshot({ path: captureFramePath, animations: "disabled" });
    }
    await page.keyboard.down("w");
    await page.waitForTimeout(120);
    const nativeWPressed = await page.evaluate(() => Module.ccall(
      "InfernuxWebGetKeyState", "number", ["number"], [26],
    ) === 1);
    const pythonWPressed = await page.evaluate(() => Module.ccall(
      "InfernuxWebGetRuntimeDiagnostic", "number", ["number", "number"], [0, 26],
    ) === 1);
    await page.keyboard.up("w");
    await page.waitForTimeout(120);
    const nativeWReleased = await page.evaluate(() => Module.ccall(
      "InfernuxWebGetKeyState", "number", ["number"], [26],
    ) === 0);
    let gameplayMovement = null;
    if (trackedObject) {
      const readPosition = async () => page.evaluate((name) => [0, 1, 2].map((axis) =>
        Module.ccall(
          "InfernuxWebGetObjectPositionAxis",
          "number",
          ["string", "number"],
          [name, axis],
        )
      ), trackedObject);
      const before = await readPosition(trackedObject);
      const runtimeBefore = await page.evaluate(() => ({
        fixedPlanCount: Module.ccall(
          "InfernuxWebGetRuntimeDiagnostic", "number", ["number", "number"], [1, 0],
        ),
        updatePlanCount: Module.ccall(
          "InfernuxWebGetRuntimeDiagnostic", "number", ["number", "number"], [2, 0],
        ),
        nativePhaseDispatches: Module.ccall(
          "InfernuxWebGetRuntimeDiagnostic", "number", ["number", "number"], [3, 0],
        ),
        phaseErrors: Module.ccall(
          "InfernuxWebGetRuntimeDiagnostic", "number", ["number", "number"], [4, 0],
        ),
      }));
      if (!before.every(Number.isFinite)) {
        throw new Error(`Web Player could not find tracked object: ${trackedObject}`);
      }
      let whileHeld;
      if (movementTouch) {
        const touchSession = await page.context().newCDPSession(page);
        const stickCenter = {
          x: movementTouchGeometry.centerX,
          y: movementTouchGeometry.centerY,
          id: 91,
          radiusX: 14,
          radiusY: 12,
          force: 0.7,
        };
        const stickForward = {
          ...stickCenter,
          y: movementTouchGeometry.forwardY,
        };
        try {
          await touchSession.send("Input.dispatchTouchEvent", {
            type: "touchStart",
            touchPoints: [stickCenter],
          });
          await page.waitForTimeout(120);
          await touchSession.send("Input.dispatchTouchEvent", {
            type: "touchMove",
            touchPoints: [stickForward],
          });
          await page.waitForTimeout(1200);
          whileHeld = await readPosition(trackedObject);
        } finally {
          await touchSession.send("Input.dispatchTouchEvent", {
            type: "touchEnd",
            touchPoints: [],
          });
          await touchSession.detach();
        }
      } else {
        await page.keyboard.down(movementKey);
        try {
          await page.waitForTimeout(1200);
          whileHeld = await readPosition(trackedObject);
        } finally {
          await page.keyboard.up(movementKey);
        }
      }
      await page.waitForTimeout(120);
      const after = await readPosition(trackedObject);
      const runtimeAfter = await page.evaluate(() => ({
        nativePhaseDispatches: Module.ccall(
          "InfernuxWebGetRuntimeDiagnostic", "number", ["number", "number"], [3, 0],
        ),
        phaseErrors: Module.ccall(
          "InfernuxWebGetRuntimeDiagnostic", "number", ["number", "number"], [4, 0],
        ),
      }));
      const displacement = Math.hypot(
        after[0] - before[0],
        after[1] - before[1],
        after[2] - before[2],
      );
      const horizontalDisplacement = Math.hypot(
        after[0] - before[0],
        after[2] - before[2],
      );
      gameplayMovement = {
        object: trackedObject,
        input: movementTouch ? "touch:left-zone-forward" : `key:${movementKey}`,
        before,
        whileHeld,
        after,
        displacement,
        horizontalDisplacement,
        runtimeBefore,
        runtimeAfter,
        geometry: movementTouch ? movementTouchGeometry : null,
      };
    }
    let frameBeforeActivation = null;
    let sceneFrame = null;
    let shadowDifference = null;
    let skyDifference = null;
    if (!skipFrameChecks) {
      await page.waitForTimeout(250);
      frameBeforeActivation = await measureCanvasFrame(canvas);
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
      sceneFrame = summarizeCanvasFrame(featureBaseline);
      shadowDifference = compareCanvasFrames(featureBaseline, shadowsDisabled);
      skyDifference = compareCanvasFrames(featureBaseline, skyDisabled);
      await page.evaluate(() => {
        const loader = document.querySelector("#infernux-loader");
        if (loader) loader.style.visibility = "";
      });
    }
    await activateCanvas(page, canvasBox, cdpEndpoint);
    await page.waitForFunction(() => {
      const diagnostics = JSON.parse(
        document.querySelector("#canvas")?.dataset.infernuxDiagnostics || "[]",
      );
      return diagnostics.some((item) => item.includes("INFERNUX_WEB_AUDIO_READY"));
    }, null, { timeout: 30000 });
    await page.waitForTimeout(250);
    const frameAfterActivation = skipFrameChecks ? null : await measureCanvasFrame(canvas);
    let nativeMultitouch = null;
    if (verifyNativeMultitouch) {
      const touchSession = await page.context().newCDPSession(page);
      const firstStart = {
        x: canvasBox.x + canvasBox.width * 0.18,
        y: canvasBox.y + canvasBox.height * 0.72,
        id: 301,
        radiusX: 13,
        radiusY: 11,
        force: 0.55,
      };
      const secondStart = {
        x: canvasBox.x + canvasBox.width * 0.82,
        y: canvasBox.y + canvasBox.height * 0.72,
        id: 302,
        radiusX: 15,
        radiusY: 12,
        force: 0.7,
      };
      const firstMoved = {
        ...firstStart,
        x: firstStart.x + canvasBox.width * 0.08,
        y: firstStart.y - canvasBox.height * 0.08,
      };
      const secondMoved = {
        ...secondStart,
        x: secondStart.x - canvasBox.width * 0.08,
        y: secondStart.y - canvasBox.height * 0.08,
      };
      const readUnityTouches = async () => page.evaluate(() => {
        const diagnostic = (probe, argument = 0) => Module.ccall(
          "InfernuxWebGetRuntimeDiagnostic",
          "number",
          ["number", "number"],
          [probe, argument],
        );
        const count = Math.trunc(diagnostic(5));
        return {
          count,
          touches: Array.from({ length: count }, (_, index) => ({
            fingerId: diagnostic(6, index),
            normalizedX: diagnostic(7, index),
            normalizedY: diagnostic(8, index),
            isPrimary: diagnostic(9, index) === 1,
            phase: diagnostic(10, index),
          })),
        };
      });
      const waitForNoUnityTouches = async () => {
        await page.waitForFunction(() => Module.ccall(
          "InfernuxWebGetRuntimeDiagnostic",
          "number",
          ["number", "number"],
          [5, 0],
        ) === 0, null, { timeout: 10000 });
        return readUnityTouches();
      };
      let contactsActive = false;
      try {
        const before = await waitForNoUnityTouches();
        await touchSession.send("Input.dispatchTouchEvent", {
          type: "touchStart",
          touchPoints: [firstStart, secondStart],
        });
        contactsActive = true;
        await page.waitForTimeout(80);
        const started = await readUnityTouches();
        await touchSession.send("Input.dispatchTouchEvent", {
          type: "touchMove",
          touchPoints: [firstMoved, secondMoved],
        });
        await page.waitForTimeout(80);
        const moved = await readUnityTouches();
        await touchSession.send("Input.dispatchTouchEvent", {
          type: "touchCancel",
          touchPoints: [],
        });
        contactsActive = false;
        await page.waitForTimeout(20);
        const canceled = await readUnityTouches();
        const cleared = await waitForNoUnityTouches();
        const startedIds = started.touches.map((touch) => touch.fingerId);
        const movedIds = moved.touches.map((touch) => touch.fingerId);
        const canceledDiagnostics = await page.evaluate(() => JSON.parse(
          document.querySelector("#canvas")?.dataset.infernuxDiagnostics || "[]",
        ).filter((item) => (
          item.includes("INFERNUX_WEB_TOUCH_END") && item.includes("phase=canceled")
        )));
        const canceledIdsObserved = startedIds.every((id) => (
          canceledDiagnostics.some((item) => item.includes(`finger=${id} phase=canceled`))
        ));
        const movedDistances = moved.touches.map((touch, index) => Math.hypot(
          touch.normalizedX - started.touches[index].normalizedX,
          touch.normalizedY - started.touches[index].normalizedY,
        ));
        nativeMultitouch = {
          before,
          started,
          moved,
          canceled,
          cleared,
          canceledDiagnostics,
          movedDistances,
          passed: (
            before.count === 0 &&
            started.count === 2 && moved.count === 2 &&
            new Set(startedIds).size === 2 &&
            startedIds.every((id, index) => id === movedIds[index]) &&
            started.touches.filter((touch) => touch.isPrimary).length === 1 &&
            moved.touches.filter((touch) => touch.isPrimary).length === 1 &&
            started.touches.every((touch) => (
              touch.normalizedX >= 0 && touch.normalizedX <= 1 &&
              touch.normalizedY >= 0 && touch.normalizedY <= 1
            )) &&
            movedDistances.every((distance) => distance >= 0.05) &&
            canceledIdsObserved &&
            cleared.count === 0
          ),
        };
      } finally {
        if (contactsActive) {
          await touchSession.send("Input.dispatchTouchEvent", {
            type: "touchCancel",
            touchPoints: [],
          });
        }
        await touchSession.detach();
      }
    }
    let mobileIme = null;
    if (verifyMobileIme) {
      const imeSession = await page.context().newCDPSession(page);
      const committedText = "输入测试中文🙂";
      const before = await page.evaluate(() => ({
        innerHeight: window.innerHeight,
        visualViewportHeight: window.visualViewport.height,
      }));
      const textProbe = {
        x: canvasBox.x + canvasBox.width * 0.82,
        y: canvasBox.y + canvasBox.height * 0.18,
        id: 401,
        radiusX: 13,
        radiusY: 11,
        force: 0.65,
      };
      try {
        await imeSession.send("Input.dispatchTouchEvent", {
          type: "touchStart",
          touchPoints: [textProbe],
        });
        await imeSession.send("Input.dispatchTouchEvent", {
          type: "touchEnd",
          touchPoints: [],
        });
        await page.waitForFunction(() => (
          document.activeElement?.id === "infernux-text-input" &&
          window.visualViewport.height < window.innerHeight - 1
        ), null, { timeout: 15000 });
        const visible = await page.evaluate(() => ({
          activeElement: document.activeElement?.id || "",
          innerHeight: window.innerHeight,
          visualViewportHeight: window.visualViewport.height,
          visualViewportOffsetTop: window.visualViewport.offsetTop,
        }));
        await imeSession.send("Input.insertText", { text: committedText });
        await page.waitForFunction((expected) => {
          const diagnostics = JSON.parse(
            document.querySelector("#canvas")?.dataset.infernuxDiagnostics || "[]",
          );
          return diagnostics.some((item) => (
            item.includes("BALANCE // TEXT INPUT") && item.includes(expected)
          ));
        }, committedText, { timeout: 10000 });
        await imeSession.send("Input.dispatchKeyEvent", {
          type: "keyDown",
          key: "Enter",
          code: "Enter",
          windowsVirtualKeyCode: 13,
          nativeVirtualKeyCode: 13,
        });
        await imeSession.send("Input.dispatchKeyEvent", {
          type: "keyUp",
          key: "Enter",
          code: "Enter",
          windowsVirtualKeyCode: 13,
          nativeVirtualKeyCode: 13,
        });
        await page.waitForFunction((expected) => {
          const diagnostics = JSON.parse(
            document.querySelector("#canvas")?.dataset.infernuxDiagnostics || "[]",
          );
          return document.activeElement?.id !== "infernux-text-input" &&
            diagnostics.some((item) => (
              item.includes("BALANCE // TEXT COMMIT") && item.includes(expected)
            ));
        }, committedText, { timeout: 10000 });
        await page.waitForFunction((height) => (
          window.visualViewport.height >= height - 1
        ), before.visualViewportHeight, { timeout: 10000 });
        const after = await page.evaluate(() => ({
          activeElement: document.activeElement?.id || "",
          innerHeight: window.innerHeight,
          visualViewportHeight: window.visualViewport.height,
          visualViewportOffsetTop: window.visualViewport.offsetTop,
          diagnostics: JSON.parse(
            document.querySelector("#canvas")?.dataset.infernuxDiagnostics || "[]",
          ).filter((item) => (
            item.includes("BALANCE // TEXT") ||
            item.includes("BALANCE // KEYBOARD INSET")
          )),
        }));
        mobileIme = {
          before,
          visible,
          after,
          committedText,
          passed: (
            visible.activeElement === "infernux-text-input" &&
            visible.visualViewportHeight < visible.innerHeight - 1 &&
            after.activeElement !== "infernux-text-input" &&
            after.visualViewportHeight >= before.visualViewportHeight - 1 &&
            after.diagnostics.some((item) => (
              item.includes("BALANCE // TEXT COMMIT") && item.includes(committedText)
            ))
          ),
        };
      } finally {
        await imeSession.detach();
      }
    }
    const contextMenuPrevented = await page.evaluate((injectSyntheticText) => {
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

      if (injectSyntheticText) {
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
      }
      Module.ccall("InfernuxWebPageLifecycle", null, ["number"], [0]);
      Module.ccall("InfernuxWebPageLifecycle", null, ["number"], [1]);
      return contextMenu.defaultPrevented;
    }, !verifyMobileIme);
    await page.waitForTimeout(1000);
    const frameAfterInput = skipFrameChecks ? null : await measureCanvasFrame(canvas);
    const result = await page.evaluate((contract) => {
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
        requiredDiagnostics: Object.fromEntries(
          contract.requirements.map((requirement) => [
            requirement,
            diagnostics.find((item) => item.includes(requirement)) || "",
          ]),
        ),
        requiredDiagnosticOrders: contract.orders.map(({ before, after }) => {
          const beforeIndex = diagnostics.findIndex((item) => item.includes(before));
          const afterIndex = diagnostics.findIndex((item) => item.includes(after));
          return {
            before,
            after,
            beforeIndex,
            afterIndex,
            ordered: beforeIndex >= 0 && afterIndex > beforeIndex,
          };
        }),
        forbiddenDiagnostics: Object.fromEntries(
          contract.forbidden.map((requirement) => [
            requirement,
            diagnostics.find((item) => item.includes(requirement)) || "",
          ]),
        ),
        diagnosticTail: diagnostics.slice(-80),
      };
    }, {
      requirements: requiredDiagnostics,
      orders: requiredDiagnosticOrders,
      forbidden: forbiddenDiagnostics,
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
    result.pythonWPressed = pythonWPressed;
    result.gameplayMovement = gameplayMovement;
    result.nativeMultitouch = nativeMultitouch;
    result.mobileIme = mobileIme;
    if (captureFramePath) result.captureFramePath = captureFramePath;
    const frameIsVisible = (frame) => frame && (
      frame.nonBlackRatio >= 0.1 &&
      frame.luminanceDeviation >= 0.01 &&
      frame.quantizedColorCount >= 8
    );
    const inputPreservedFrame = skipFrameChecks || (
      frameAfterInput.meanLuminance >= Math.max(
        0.01,
        frameAfterActivation.meanLuminance * 0.05,
      )
    );
    const skyIsVisible = skipFrameChecks || (
      skyDifference.changedPixelRatio >= 0.2 &&
      skyDifference.meanAbsoluteDifference >= 0.03
    );
    const shadowsAreVisible = skipFrameChecks || (
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
        !result.nativeWPressed || !result.nativeWReleased || !result.pythonWPressed ||
        (result.gameplayMovement &&
          result.gameplayMovement.horizontalDisplacement < minimumDisplacement) ||
        (verifyNativeMultitouch && !result.nativeMultitouch?.passed) ||
        (verifyMobileIme && !result.mobileIme?.passed) ||
        Object.values(result.requiredDiagnostics).some((match) => !match) ||
        result.requiredDiagnosticOrders.some((order) => !order.ordered) ||
        Object.values(result.forbiddenDiagnostics).some((match) => match) ||
        !result.audioReady || !result.audioContextRunning ||
        (requireActiveAudio && result.activeAudioVoices < 1) ||
        !result.pointerDown || !result.pointerCancel || !result.textInput ||
        !result.pageHide || !result.pageShow ||
        stateBeforeActivation !== "ready" || !result.contextMenuPrevented ||
        !presentationMatches || !renderSizeMatches || !centeredWindowMatches ||
        (!skipFrameChecks && !frameIsVisible(frameBeforeActivation)) ||
        (!skipFrameChecks && !frameIsVisible(frameAfterActivation)) ||
        (!skipFrameChecks && !frameIsVisible(frameAfterInput)) ||
        !inputPreservedFrame || !skyIsVisible || !shadowsAreVisible) {
      throw new Error(JSON.stringify({ result, pageErrors, consoleErrors }));
    }
    delete result.diagnosticTail;
    writeJsonAtomic(reportPath, {
      schema: 1,
      status: "passed",
      url,
      elapsed_seconds: Number(process.hrtime.bigint() - startedAt) / 1e9,
      result,
    });
    process.stdout.write(`${JSON.stringify(result)}\n`);
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
