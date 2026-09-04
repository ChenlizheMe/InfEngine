#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <python-executable>" >&2
    exit 2
fi

python_executable="$1"
if [[ ! -x "$python_executable" ]]; then
    echo "Python executable is unavailable: $python_executable" >&2
    exit 2
fi
for variable in INFERNUX_ANDROID_RUNTIME INFERNUX_ANDROID_BUILD_CACHE; do
    if [[ -z "${!variable:-}" ]]; then
        echo "Required environment variable is unset: $variable" >&2
        exit 2
    fi
done

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

wait_for_android_input_service

adb -s emulator-5554 shell svc power stayon true
# This driver owns a disposable CI emulator. Remove its non-secure keyguard
# before the long native build so API 36 cannot return to a locked SystemUI
# state while no window is attached.
adb -s emulator-5554 shell locksettings set-disabled true
adb -s emulator-5554 shell input keyevent KEYCODE_WAKEUP
adb -s emulator-5554 shell wm dismiss-keyguard
adb -s emulator-5554 shell input keyevent KEYCODE_HOME

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

# A long native build can outlive an emulator framework restart. Re-establish
# the same concrete service barrier before sending any synthetic input.
wait_for_android_input_service
adb -s emulator-5554 shell locksettings set-disabled true
adb -s emulator-5554 shell svc power stayon true
adb -s emulator-5554 shell input keyevent KEYCODE_WAKEUP
adb -s emulator-5554 shell wm dismiss-keyguard
adb -s emulator-5554 shell input keyevent KEYCODE_HOME

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
    --report out/test-results/android-multitouch-smoke.json
