package com.infernux.acceptance.input;

import android.app.Activity;
import android.app.Instrumentation;
import android.app.UiAutomation;
import android.content.ComponentName;
import android.content.Intent;
import android.graphics.Rect;
import android.os.Bundle;
import android.os.ParcelFileDescriptor;
import android.os.SystemClock;
import android.view.InputDevice;
import android.view.MotionEvent;
import android.view.WindowManager;

import java.io.FileInputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;

/** Injects two simultaneous contacts through Android's system input test boundary. */
public final class MultiTouchInstrumentation extends Instrumentation {
    private static final int SECOND_POINTER_ACTION =
            MotionEvent.ACTION_POINTER_DOWN | (1 << MotionEvent.ACTION_POINTER_INDEX_SHIFT);
    private static final int SECOND_POINTER_UP_ACTION =
            MotionEvent.ACTION_POINTER_UP | (1 << MotionEvent.ACTION_POINTER_INDEX_SHIFT);

    private Bundle arguments;

    @Override
    public void onCreate(Bundle arguments) {
        this.arguments = arguments == null ? Bundle.EMPTY : arguments;
        super.onCreate(arguments);
        start();
    }

    @Override
    public void onStart() {
        final Bundle result = new Bundle();
        try {
            setInTouchMode(true);
            final String targetPackage = getTargetContext().getPackageName();
            final Intent launch = getTargetContext().getPackageManager().getLaunchIntentForPackage(targetPackage);
            if (launch == null) {
                throw new IllegalStateException("No launcher activity for " + targetPackage);
            }
            final ComponentName component = launch.getComponent();
            if (component == null) {
                throw new IllegalStateException("Launcher intent has no component for " + targetPackage);
            }
            final UiAutomation automation = getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES);
            launchFromShell(automation, component);
            final long waitMilliseconds = readPositiveLong("waitMilliseconds", 7000L);
            SystemClock.sleep(waitMilliseconds);

            final WindowManager windowManager = getTargetContext().getSystemService(WindowManager.class);
            if (windowManager == null) {
                throw new IllegalStateException("Target context has no WindowManager");
            }
            final Rect bounds = windowManager.getCurrentWindowMetrics().getBounds();
            final int width = bounds.width();
            final int height = bounds.height();
            if (width <= 0 || height <= 0) {
                throw new IllegalStateException("Target activity has no drawable extent");
            }

            injectCompletedGesture(automation, width, height);
            SystemClock.sleep(350L);
            injectCanceledGesture(automation, width, height);
            SystemClock.sleep(350L);

            result.putString("INFERNUX_MULTITOUCH_INJECTION", "passed");
            result.putInt("width", width);
            result.putInt("height", height);
            finish(Activity.RESULT_OK, result);
        } catch (Throwable error) {
            result.putString("INFERNUX_MULTITOUCH_INJECTION", "failed");
            result.putString("error", error.toString());
            finish(Activity.RESULT_CANCELED, result);
        }
    }

    private static void launchFromShell(UiAutomation automation, ComponentName component)
            throws IOException {
        final String command = "am start -W -n " + component.flattenToShortString();
        try (ParcelFileDescriptor descriptor = automation.executeShellCommand(command);
                FileInputStream input = new FileInputStream(descriptor.getFileDescriptor())) {
            final String output = new String(input.readAllBytes(), StandardCharsets.UTF_8);
            if (!output.contains("Status: ok")) {
                throw new IllegalStateException("Android failed to launch target activity: " + output.trim());
            }
        }
    }

    private long readPositiveLong(String key, long defaultValue) {
        final String value = arguments.getString(key);
        if (value == null) {
            return defaultValue;
        }
        final long parsed = Long.parseLong(value);
        if (parsed <= 0L) {
            throw new IllegalArgumentException(key + " must be positive");
        }
        return parsed;
    }

    private static void injectCompletedGesture(UiAutomation automation, int width, int height) {
        final long downTime = SystemClock.uptimeMillis();
        inject(automation, downTime, MotionEvent.ACTION_DOWN, width, height, 1, 0.11f, 0.82f, 0.0f, 0.0f);
        inject(automation, downTime, SECOND_POINTER_ACTION, width, height, 2, 0.11f, 0.82f, 0.84f, 0.80f);
        SystemClock.sleep(120L);
        inject(automation, downTime, MotionEvent.ACTION_MOVE, width, height, 2, 0.18f, 0.70f, 0.80f, 0.72f);
        SystemClock.sleep(120L);
        inject(automation, downTime, SECOND_POINTER_UP_ACTION, width, height, 2, 0.18f, 0.70f, 0.80f, 0.72f);
        inject(automation, downTime, MotionEvent.ACTION_UP, width, height, 1, 0.18f, 0.70f, 0.0f, 0.0f);
    }

    private static void injectCanceledGesture(UiAutomation automation, int width, int height) {
        final long downTime = SystemClock.uptimeMillis();
        inject(automation, downTime, MotionEvent.ACTION_DOWN, width, height, 1, 0.12f, 0.81f, 0.0f, 0.0f);
        inject(automation, downTime, SECOND_POINTER_ACTION, width, height, 2, 0.12f, 0.81f, 0.83f, 0.79f);
        SystemClock.sleep(100L);
        inject(automation, downTime, MotionEvent.ACTION_CANCEL, width, height, 2, 0.12f, 0.81f, 0.83f, 0.79f);
    }

    private static void inject(
            UiAutomation automation,
            long downTime,
            int action,
            int width,
            int height,
            int pointerCount,
            float firstX,
            float firstY,
            float secondX,
            float secondY) {
        final MotionEvent.PointerProperties[] properties = new MotionEvent.PointerProperties[pointerCount];
        final MotionEvent.PointerCoords[] coordinates = new MotionEvent.PointerCoords[pointerCount];
        for (int index = 0; index < pointerCount; ++index) {
            final MotionEvent.PointerProperties pointer = new MotionEvent.PointerProperties();
            pointer.id = index;
            pointer.toolType = MotionEvent.TOOL_TYPE_FINGER;
            properties[index] = pointer;

            final MotionEvent.PointerCoords coordinate = new MotionEvent.PointerCoords();
            final float normalizedX = index == 0 ? firstX : secondX;
            final float normalizedY = index == 0 ? firstY : secondY;
            coordinate.x = normalizedX * width;
            coordinate.y = normalizedY * height;
            coordinate.pressure = 1.0f;
            coordinate.size = 0.08f;
            coordinates[index] = coordinate;
        }

        final MotionEvent event = MotionEvent.obtain(
                downTime,
                SystemClock.uptimeMillis(),
                action,
                pointerCount,
                properties,
                coordinates,
                0,
                0,
                1.0f,
                1.0f,
                0,
                0,
                InputDevice.SOURCE_TOUCHSCREEN,
                0);
        try {
            if (!automation.injectInputEvent(event, true)) {
                throw new IllegalStateException("Android rejected motion action " + action);
            }
        } finally {
            event.recycle();
        }
    }
}
