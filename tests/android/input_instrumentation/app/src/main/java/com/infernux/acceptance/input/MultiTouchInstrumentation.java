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
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowManager;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputConnection;

import java.io.FileInputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;

/** Injects two simultaneous contacts through Android's system input test boundary. */
public final class MultiTouchInstrumentation extends Instrumentation {
    private static final String EXPECTED_TEXT = "输入测试中文🙂";
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
        UiAutomation automation = null;
        String originalKeyboardSetting = null;
        boolean keyboardSettingChanged = false;
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
            automation = getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES);
            originalKeyboardSetting = shell(
                    automation, "settings get secure show_ime_with_hard_keyboard").trim();
            if (!originalKeyboardSetting.equals("0")
                    && !originalKeyboardSetting.equals("1")
                    && !originalKeyboardSetting.equals("null")) {
                throw new IllegalStateException(
                        "Unexpected show_ime_with_hard_keyboard value: "
                                + originalKeyboardSetting);
            }
            shell(automation, "settings put secure show_ime_with_hard_keyboard 1");
            keyboardSettingChanged = true;
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

            injectTap(automation, width, height, 0.5f, 0.07f);
            final Activity targetActivity = waitForTargetActivity(10000L);
            final ImeSnapshot visibleIme = waitForIme(targetActivity, true, 10000L);
            if (!visibleIme.editorFocused) {
                throw new IllegalStateException(
                        "Android IME is visible without an SDL text editor focus");
            }
            commitText(targetActivity, EXPECTED_TEXT);
            waitForIme(targetActivity, false, 10000L);

            restoreKeyboardSetting(automation, originalKeyboardSetting);
            keyboardSettingChanged = false;

            result.putString("INFERNUX_MULTITOUCH_INJECTION", "passed");
            result.putString("INFERNUX_IME_INJECTION", "passed");
            result.putString("committedText", EXPECTED_TEXT);
            result.putInt("imeInset", visibleIme.inset);
            result.putInt("width", width);
            result.putInt("height", height);
            finish(Activity.RESULT_OK, result);
        } catch (Throwable error) {
            if (keyboardSettingChanged && automation != null) {
                try {
                    restoreKeyboardSetting(automation, originalKeyboardSetting);
                } catch (Throwable cleanupError) {
                    error.addSuppressed(cleanupError);
                }
            }
            result.putString("INFERNUX_MULTITOUCH_INJECTION", "failed");
            result.putString("INFERNUX_IME_INJECTION", "failed");
            result.putString("error", error.toString());
            finish(Activity.RESULT_CANCELED, result);
        }
    }

    private static void launchFromShell(UiAutomation automation, ComponentName component)
            throws IOException {
        final String command = "am start -W -n " + component.flattenToShortString();
        final String output = shell(automation, command);
        if (!output.contains("Status: ok")) {
            throw new IllegalStateException(
                    "Android failed to launch target activity: " + output.trim());
        }
    }

    private static String shell(UiAutomation automation, String command) throws IOException {
        try (ParcelFileDescriptor descriptor = automation.executeShellCommand(command);
                FileInputStream input = new FileInputStream(descriptor.getFileDescriptor())) {
            return new String(input.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    private static void restoreKeyboardSetting(
            UiAutomation automation, String originalValue) throws IOException {
        if ("null".equals(originalValue)) {
            shell(automation, "settings delete secure show_ime_with_hard_keyboard");
            return;
        }
        shell(
                automation,
                "settings put secure show_ime_with_hard_keyboard " + originalValue);
    }

    private Activity waitForTargetActivity(long timeoutMilliseconds) throws Exception {
        final long deadline = SystemClock.uptimeMillis() + timeoutMilliseconds;
        while (SystemClock.uptimeMillis() < deadline) {
            final Class<?> activityClass = Class.forName("org.libsdl.app.SDLActivity");
            final Object context = activityClass.getMethod("getContext").invoke(null);
            if (context instanceof Activity) {
                return (Activity) context;
            }
            SystemClock.sleep(50L);
        }
        throw new IllegalStateException("SDL target Activity did not become available");
    }

    private ImeSnapshot waitForIme(
            Activity activity, boolean expectedVisible, long timeoutMilliseconds) {
        final long deadline = SystemClock.uptimeMillis() + timeoutMilliseconds;
        ImeSnapshot snapshot = null;
        while (SystemClock.uptimeMillis() < deadline) {
            snapshot = readImeSnapshot(activity);
            if (snapshot.visible == expectedVisible) {
                if (!expectedVisible || snapshot.inset > 0) {
                    return snapshot;
                }
            }
            SystemClock.sleep(50L);
        }
        throw new IllegalStateException(
                "Android IME visibility did not become " + expectedVisible
                        + "; last=" + snapshot);
    }

    private ImeSnapshot readImeSnapshot(Activity activity) {
        final ImeSnapshot[] result = new ImeSnapshot[1];
        runOnMainSync(() -> {
            final View decorView = activity.getWindow().getDecorView();
            final WindowInsets insets = decorView.getRootWindowInsets();
            if (insets == null) {
                result[0] = new ImeSnapshot(false, 0, false);
                return;
            }
            final boolean visible = insets.isVisible(WindowInsets.Type.ime());
            final int inset = insets.getInsets(WindowInsets.Type.ime()).bottom;
            final View focused = activity.getCurrentFocus();
            result[0] = new ImeSnapshot(
                    visible,
                    inset,
                    focused != null && focused.onCheckIsTextEditor());
        });
        return result[0];
    }

    private void commitText(Activity activity, String text) {
        final boolean[] committed = new boolean[1];
        runOnMainSync(() -> {
            final View focused = activity.getCurrentFocus();
            if (focused == null || !focused.onCheckIsTextEditor()) {
                throw new IllegalStateException("SDL text editor is not focused");
            }
            final InputConnection connection =
                    focused.onCreateInputConnection(new EditorInfo());
            if (connection == null) {
                throw new IllegalStateException("SDL text editor has no InputConnection");
            }
            committed[0] = connection.commitText(text, 1);
        });
        if (!committed[0]) {
            throw new IllegalStateException("Android InputConnection rejected committed text");
        }
    }

    private static final class ImeSnapshot {
        final boolean visible;
        final int inset;
        final boolean editorFocused;

        ImeSnapshot(boolean visible, int inset, boolean editorFocused) {
            this.visible = visible;
            this.inset = inset;
            this.editorFocused = editorFocused;
        }

        @Override
        public String toString() {
            return "ImeSnapshot{visible=" + visible
                    + ", inset=" + inset
                    + ", editorFocused=" + editorFocused + "}";
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

    private static void injectTap(
            UiAutomation automation, int width, int height, float x, float y) {
        final long downTime = SystemClock.uptimeMillis();
        inject(automation, downTime, MotionEvent.ACTION_DOWN, width, height, 1, x, y, 0.0f, 0.0f);
        SystemClock.sleep(120L);
        inject(automation, downTime, MotionEvent.ACTION_UP, width, height, 1, x, y, 0.0f, 0.0f);
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
