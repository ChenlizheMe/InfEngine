package com.infernux.bootstrap;

import android.os.Bundle;
import android.system.ErrnoException;
import android.system.Os;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileNotFoundException;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;

import org.libsdl.app.SDLActivity;

public final class InfernuxActivity extends SDLActivity {
    private static final String PYTHON_ASSET_ROOT = "python";
    private static final String PYTHON_RUNTIME_ID = "infernux-runtime.id";
    private static final String PLAYER_ASSET_ROOT = "player";
    private static final String PLAYER_CONTENT_ID = "infernux-content.id";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        try {
            File pythonHome = prepareVersionedAssets(
                    PYTHON_ASSET_ROOT,
                    PYTHON_RUNTIME_ID);
            File playerAssets = prepareVersionedAssets(
                    PLAYER_ASSET_ROOT,
                    PLAYER_CONTENT_ID);
            Os.setenv("INFERNUX_PYTHON_HOME", pythonHome.getAbsolutePath(), true);
            Os.setenv(
                    "INFERNUX_NATIVE_LIBRARY_DIR",
                    getApplicationInfo().nativeLibraryDir,
                    true);
            Os.setenv(
                    "INFERNUX_PLAYER_ASSET_ROOT",
                    playerAssets.getAbsolutePath(),
                    true);
            Os.setenv(
                    "INFERNUX_PLAYER_CACHE_ROOT",
                    new File(getCacheDir(), "player").getAbsolutePath(),
                    true);
            Os.setenv("TMPDIR", getCacheDir().getAbsolutePath(), true);
        } catch (IOException | ErrnoException exception) {
            throw new IllegalStateException("Failed to prepare embedded Python", exception);
        }
        super.onCreate(savedInstanceState);
    }

    private File prepareVersionedAssets(String assetRoot, String identityName)
            throws IOException {
        File installedRoot = new File(getFilesDir(), assetRoot);
        File installedIdentity = new File(installedRoot, identityName);
        String packagedIdentity = readUtf8(getAssets().open(
                assetRoot + "/" + identityName));
        String currentIdentity = installedIdentity.isFile()
                ? readUtf8(new FileInputStream(installedIdentity))
                : "";
        if (!packagedIdentity.equals(currentIdentity)) {
            deleteRecursively(installedRoot);
            extractAsset(assetRoot, getFilesDir());
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
