package com.infernux.bootstrap;

import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.graphics.Insets;
import android.os.Bundle;
import android.os.Build;
import android.system.ErrnoException;
import android.system.Os;
import android.util.Log;
import android.view.KeyEvent;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowInsetsAnimation;
import android.view.WindowInsetsController;
import android.window.OnBackInvokedDispatcher;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileNotFoundException;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Locale;

import org.libsdl.app.SDLActivity;

public final class InfernuxActivity extends SDLActivity {
    private static final String LOG_TAG = "InfernuxActivity";
    private static final String PYTHON_ASSET_ROOT = "python";
    private static final String PYTHON_RUNTIME_ID = "infernux-runtime.id";
    private static final String PLAYER_ASSET_ROOT = "player";
    private static final String PLAYER_CONTENT_ID = "infernux-content.id";
    private static final String PLAYER_DATA_ROOT = "infernux-data-root.txt";
    private int lastPublishedKeyboardInset = Integer.MIN_VALUE;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        try {
            File pythonHome = prepareVersionedAssets(
                    PYTHON_ASSET_ROOT,
                    PYTHON_RUNTIME_ID);
            File playerAssets = prepareVersionedAssets(
                    PLAYER_ASSET_ROOT,
                    PLAYER_CONTENT_ID);
            File playerData = resolvePlayerDataRoot(playerAssets);
            Os.setenv("INFERNUX_PYTHON_HOME", pythonHome.getAbsolutePath(), true);
            Os.setenv(
                    "INFERNUX_NATIVE_LIBRARY_DIR",
                    getApplicationInfo().nativeLibraryDir,
                    true);
            Os.setenv(
                    "INFERNUX_PLAYER_ASSET_ROOT",
                    playerData.getAbsolutePath(),
                    true);
            Os.setenv(
                    "INFERNUX_PLAYER_CACHE_ROOT",
                    new File(getCacheDir(), "player").getAbsolutePath(),
                    true);
            Os.setenv("INFERNUX_RENDER_PROFILE", "mobile", true);
            Os.setenv("INFERNUX_PRESENT_MODE", "fifo", true);
            Os.setenv("INFERNUX_MAX_FRAMES_IN_FLIGHT", "2", true);
            if (getIntent() != null
                    && getIntent().getBooleanExtra("infernux.profile_frames", false)) {
                Os.setenv("_INFERNUX_PLAYER_PROFILE_FRAMES", "1", true);
                Log.i(LOG_TAG, "INFERNUX_ANDROID_FRAME_PROFILE enabled by launch intent");
            }
            configureResolutionScaling();
            Os.setenv("TMPDIR", getCacheDir().getAbsolutePath(), true);
        } catch (IOException | ErrnoException | PackageManager.NameNotFoundException exception) {
            throw new IllegalStateException("Failed to prepare embedded Python", exception);
        }
        super.onCreate(savedInstanceState);
        applyImmersiveGameMode();
        installWindowInsetsBridge();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
                    OnBackInvokedDispatcher.PRIORITY_DEFAULT,
                    this::dispatchInfernuxBack);
        }
    }

    private static File resolvePlayerDataRoot(File playerAssets) throws IOException {
        File descriptor = new File(playerAssets, PLAYER_DATA_ROOT);
        if (!descriptor.isFile()) {
            throw new IOException("Android Player data-root descriptor is missing");
        }
        String directoryName = readUtf8(new FileInputStream(descriptor));
        if (!directoryName.endsWith("_Data")
                || directoryName.contains("/")
                || directoryName.contains("\\")) {
            throw new IOException("Android Player data-root descriptor is invalid");
        }
        File playerData = new File(playerAssets, directoryName);
        if (!playerData.isDirectory()
                || !new File(playerData, "Player.inxmanifest").isFile()) {
            throw new IOException("Android Player cooked Data directory is incomplete");
        }
        return playerData;
    }

    private void configureResolutionScaling()
            throws ErrnoException, PackageManager.NameNotFoundException {
        ApplicationInfo applicationInfo = getPackageManager().getApplicationInfo(
                getPackageName(),
                PackageManager.GET_META_DATA);
        Bundle metadata = applicationInfo.metaData;
        String mode = metadata != null
                ? metadata.getString("infernux.resolution_scaling", "fixed_dpi")
                : "fixed_dpi";
        int targetDpi = metadata != null
                ? metadata.getInt("infernux.target_dpi", 320)
                : 320;
        int deviceDpi = Math.max(1, getResources().getDisplayMetrics().densityDpi);
        float renderScale = "fixed_dpi".equals(mode)
                ? Math.min((float) targetDpi / (float) deviceDpi, 1.0f)
                : 1.0f;
        String scaleText = String.format(Locale.ROOT, "%.6f", renderScale);
        Os.setenv("INFERNUX_PLAYER_RENDER_SCALE", scaleText, true);
        Log.i(
                LOG_TAG,
                "INFERNUX_ANDROID_RESOLUTION_SCALING mode=" + mode
                        + " target_dpi=" + targetDpi
                        + " device_dpi=" + deviceDpi
                        + " scale=" + scaleText);
    }

    @Override
    protected void onResume() {
        super.onResume();
        applyImmersiveGameMode();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) {
            applyImmersiveGameMode();
        }
    }

    @SuppressWarnings("deprecation")
    private void applyImmersiveGameMode() {
        View decorView = getWindow().getDecorView();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            getWindow().setDecorFitsSystemWindows(false);
            WindowInsetsController controller = getWindow().getInsetsController();
            if (controller != null) {
                controller.hide(
                        WindowInsets.Type.statusBars()
                                | WindowInsets.Type.navigationBars());
                controller.setSystemBarsBehavior(
                        WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            }
            return;
        }
        decorView.setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_STABLE);
    }

    private void installWindowInsetsBridge() {
        View decorView = getWindow().getDecorView();
        decorView.setOnApplyWindowInsetsListener((view, windowInsets) -> {
            publishKeyboardInset(windowInsets);
            return windowInsets;
        });
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            decorView.setWindowInsetsAnimationCallback(
                    new WindowInsetsAnimation.Callback(
                            WindowInsetsAnimation.Callback.DISPATCH_MODE_CONTINUE_ON_SUBTREE) {
                        @Override
                        public WindowInsets onProgress(
                                WindowInsets windowInsets,
                                List<WindowInsetsAnimation> runningAnimations) {
                            publishKeyboardInset(windowInsets);
                            return windowInsets;
                        }
                    });
        }
        decorView.requestApplyInsets();
    }

    private void publishKeyboardInset(WindowInsets windowInsets) {
        int keyboardInset = 0;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            Insets ime = windowInsets.getInsets(WindowInsets.Type.ime());
            Insets systemBars = windowInsets.getInsets(WindowInsets.Type.systemBars());
            if (windowInsets.isVisible(WindowInsets.Type.ime())) {
                keyboardInset = Math.max(0, ime.bottom - systemBars.bottom);
            }
        } else {
            keyboardInset = Math.max(
                    0,
                    windowInsets.getSystemWindowInsetBottom()
                            - windowInsets.getStableInsetBottom());
        }
        if (keyboardInset == lastPublishedKeyboardInset) {
            return;
        }
        try {
            Os.setenv("INFERNUX_ANDROID_KEYBOARD_INSET", Integer.toString(keyboardInset), true);
            Os.setenv("INFERNUX_ANDROID_KEYBOARD_INSET_KNOWN", "1", true);
            lastPublishedKeyboardInset = keyboardInset;
        } catch (ErrnoException exception) {
            Log.e(LOG_TAG, "Failed to publish Android keyboard insets", exception);
        }
    }

    @Override
    public void onBackPressed() {
        dispatchInfernuxBack();
    }

    private void dispatchInfernuxBack() {
        if (mScreenKeyboardShown) {
            sendCommand(COMMAND_TEXTEDIT_HIDE, null);
            onNativeKeyboardFocusLost();
            return;
        }

        // Back is gameplay/UI input, not an unconditional Activity exit.
        // SDL maps Android KEYCODE_BACK to SDL_SCANCODE_AC_BACK, allowing the
        // portable Cancel action to close a modal, pause, or ask for exit.
        onNativeKeyDown(KeyEvent.KEYCODE_BACK);
        onNativeKeyUp(KeyEvent.KEYCODE_BACK);
    }

    private File prepareVersionedAssets(String assetRoot, String identityName)
            throws IOException {
        File installedRoot = new File(getFilesDir(), assetRoot);
        File installedIdentity = new File(installedRoot, identityName);
        File completionMarker = new File(installedRoot, identityName + ".complete");
        String packagedIdentity = readUtf8(getAssets().open(
                assetRoot + "/" + identityName));
        String currentIdentity = installedIdentity.isFile()
                ? readUtf8(new FileInputStream(installedIdentity))
                : "";
        String completedIdentity = completionMarker.isFile()
                ? readUtf8(new FileInputStream(completionMarker))
                : "";
        if (!packagedIdentity.equals(currentIdentity)
                || !packagedIdentity.equals(completedIdentity)) {
            // Never extract directly into the published runtime. Android may
            // terminate the process while an APK is being upgraded or while
            // a large Python tree is copied. A completion marker written only
            // after the staged tree is renamed makes the next launch repair an
            // interrupted install instead of trusting an identity file that
            // happened to be copied before the missing payload.
            File stagingBase = new File(getFilesDir(), assetRoot + ".installing");
            deleteRecursively(stagingBase);
            extractAsset(assetRoot, stagingBase);
            File stagedRoot = new File(stagingBase, assetRoot);
            File stagedIdentity = new File(stagedRoot, identityName);
            if (!stagedIdentity.isFile()
                    || !packagedIdentity.equals(readUtf8(new FileInputStream(stagedIdentity)))) {
                deleteRecursively(stagingBase);
                throw new IOException("Staged asset identity mismatch for " + assetRoot);
            }
            deleteRecursively(installedRoot);
            if (!stagedRoot.renameTo(installedRoot)) {
                deleteRecursively(stagingBase);
                throw new IOException("Failed to publish staged assets for " + assetRoot);
            }
            writeUtf8(completionMarker, packagedIdentity);
            deleteRecursively(stagingBase);
        }
        return installedRoot;
    }

    private static String readUtf8(InputStream input) throws IOException {
        try (InputStream source = input;
                ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[4096];
            int count;
            while ((count = source.read(buffer)) >= 0) {
                output.write(buffer, 0, count);
            }
            return new String(output.toByteArray(), StandardCharsets.UTF_8).trim();
        }
    }

    private static void writeUtf8(File output, String value) throws IOException {
        try (FileOutputStream stream = new FileOutputStream(output)) {
            stream.write((value + "\n").getBytes(StandardCharsets.UTF_8));
            stream.getFD().sync();
        }
    }

    private void extractAsset(String path, File destinationRoot) throws IOException {
        try (InputStream input = getAssets().open(path)) {
            File output = new File(destinationRoot, path);
            File parent = output.getParentFile();
            if (parent != null && !parent.isDirectory() && !parent.mkdirs()) {
                throw new IOException("Failed to create " + parent);
            }
            try (FileOutputStream stream = new FileOutputStream(output)) {
                byte[] buffer = new byte[64 * 1024];
                int count;
                while ((count = input.read(buffer)) >= 0) {
                    stream.write(buffer, 0, count);
                }
            }
            return;
        } catch (FileNotFoundException ignored) {
            // Android's AssetManager exposes directories by rejecting open().
        }

        File directory = new File(destinationRoot, path);
        if (!directory.isDirectory() && !directory.mkdirs()) {
            throw new IOException("Failed to create " + directory);
        }
        String[] children = getAssets().list(path);
        if (children == null) {
            throw new IOException("Failed to list asset directory " + path);
        }
        for (String child : children) {
            extractAsset(path + "/" + child, destinationRoot);
        }
    }

    private static void deleteRecursively(File path) throws IOException {
        if (!path.exists()) {
            return;
        }
        File[] children = path.listFiles();
        if (children != null) {
            for (File child : children) {
                deleteRecursively(child);
            }
        }
        if (!path.delete()) {
            throw new IOException("Failed to delete stale runtime path " + path);
        }
    }
}
