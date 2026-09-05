#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "Usage: $0 <python-executable> [build|smoke|all]" >&2
    exit 2
fi

python_executable="$1"
mode="${2:-all}"
if [[ ! -x "$python_executable" ]]; then
    echo "Python executable is unavailable: $python_executable" >&2
    exit 2
fi
case "$mode" in
    build|smoke|all) ;;
    *)
        echo "Unknown Android CI mode: $mode" >&2
        exit 2
        ;;
esac

if [[ "$mode" == "build" || "$mode" == "all" ]]; then
    for variable in INFERNUX_ANDROID_RUNTIME INFERNUX_ANDROID_BUILD_CACHE; do
        if [[ -z "${!variable:-}" ]]; then
            echo "Required environment variable is unset: $variable" >&2
            exit 2
        fi
    done
fi

wait_for_android_input_service() {
    local attempt
    local boot_completed
    local input_service
    adb -s emulator-5554 wait-for-device
    for attempt in $(seq 1 120); do
        boot_completed="$(adb -s emulator-5554 shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')"
        input_service="$(adb -s emulator-5554 shell service check input 2>/dev/null | tr -d '\r')"
        if [[ "$boot_completed" == "1" && "$input_service" == *"found"* ]]; then
            echo "Android framework input service is ready (attempt=$attempt)."
            return 0
        fi
        sleep 1
    done
    echo "Android framework did not publish the input service after boot." >&2
    adb -s emulator-5554 shell getprop sys.boot_completed >&2 || true
    adb -s emulator-5554 shell service check input >&2 || true
    return 1
}

if [[ "$mode" == "build" || "$mode" == "all" ]]; then
    rm -rf -- out/ci-projects/android out/acceptance/android
    mkdir -p out/ci-projects/android out/test-results
    cp -a tests/fixtures/multiplatform_player/. out/ci-projects/android/

    "$python_executable" scripts/acceptance/build_player.py \
        out/ci-projects/android android-x64-emulator out/acceptance/android \
        --report out/test-results/android-player-build.json \
        --option 'android_artifact="apk"' \
        --option "android_python_prefix=\"$INFERNUX_ANDROID_RUNTIME\"" \
        --option "build_cache_root=\"$INFERNUX_ANDROID_BUILD_CACHE\""

    gradle -p tests/android/input_instrumentation \
        --no-daemon \
        --console=plain \
        -PinfernuxTargetPackage=com.infernux.bootstrap \
        :app:assembleDebug
fi

if [[ "$mode" == "smoke" || "$mode" == "all" ]]; then
    # The emulator is deliberately launched only after all native/Gradle work
    # finishes. A software-only API 36 AVD cannot stay responsive while the
    # two-core hosted runner is saturated by the Android native build.
    wait_for_android_input_service
    adb -s emulator-5554 shell locksettings set-disabled true
    adb -s emulator-5554 shell svc power stayon true
    # A fresh AVD otherwise presents Android's first-use immersive-mode
    # confirmation above the fullscreen SDL activity. The overlay owns window
    # focus, so injected input would target SystemUI instead of the Player.
    # Android's own CTS/WindowManager suites normalize this secure setting too.
    adb -s emulator-5554 shell settings put secure immersive_mode_confirmations confirmed
    immersive_confirmation="$(
        adb -s emulator-5554 shell settings get secure immersive_mode_confirmations \
            2>/dev/null | tr -d '\r'
    )"
    if [[ "$immersive_confirmation" != "confirmed" ]]; then
        echo "Failed to suppress the Android immersive-mode confirmation overlay: $immersive_confirmation" >&2
        exit 1
    fi

    "$python_executable" scripts/acceptance/android_player_smoke.py \
        out/acceptance/android/InfernuxPlatformFixture-android-x86_64-debug.apk \
        --serial emulator-5554 \
        --no-back \
        --startup-timeout 240 \
        --expect-landscape \
        --resume-cycles 2 \
        --report out/test-results/android-player-smoke.json

    "$python_executable" scripts/acceptance/android_multitouch_smoke.py \
        tests/android/input_instrumentation/app/build/outputs/apk/debug/app-debug.apk \
        --serial emulator-5554 \
        --wait-milliseconds 20000 \
        --report out/test-results/android-multitouch-smoke.json \
        --logcat-report out/test-results/android-multitouch-smoke.logcat.txt
fi
