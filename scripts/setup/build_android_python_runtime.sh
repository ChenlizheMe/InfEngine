#!/usr/bin/env bash
set -euo pipefail

PYTHON_VERSION="3.13.15"
PYTHON_SHA256="1e66a7945a48390ee4c2a4268a0e4185884059a13c4aab6d148aa208deea4a76"
PYTHON_URL="https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz"
NUMPY_VERSION="2.5.2"
NUMPY_SHA256="d482d171c406ae88c5b19cad3b6a1c4c5209f886ab74bc44c2c865c23f52d860"
NUMPY_URL="https://files.pythonhosted.org/packages/9a/80/db0b4559e57ec36362bedbb05530a87fafbcb6067708c946967a41d449e7/numpy-${NUMPY_VERSION}.tar.gz"
CIBUILDWHEEL_VERSION="4.2.0"
CPYTHON_ANDROID_API="21"
CPYTHON_NDK_VERSION="27.3.13750724"

usage() {
    echo "Usage: $0 <arm64-v8a|x86_64> <output-prefix> [work-root]" >&2
}

if [[ $# -lt 2 || $# -gt 3 ]]; then
    usage
    exit 2
fi
if [[ -z "${ANDROID_HOME:-}" ]]; then
    echo "ANDROID_HOME must point to an Android SDK with command-line tools." >&2
    exit 2
fi
if [[ "$(uname -s)" != "Linux" ]]; then
    echo "This setup entry point currently requires a Linux build host." >&2
    exit 2
fi

abi="$1"
output_prefix="$(realpath -m "$2")"
work_root="$(realpath -m "${3:-${XDG_CACHE_HOME:-$HOME/.cache}/infernux/android-python}")"
case "$abi" in
    arm64-v8a)
        host="aarch64-linux-android"
        cibw_arch="arm64_v8a"
        minimum_api="24"
        ;;
    x86_64)
        host="x86_64-linux-android"
        cibw_arch="x86_64"
        minimum_api="26"
        ;;
    *)
        usage
        exit 2
        ;;
esac
if [[ -e "$output_prefix" ]]; then
    echo "Output prefix already exists; choose a new path: $output_prefix" >&2
    exit 2
fi

for command in curl make sha256sum tar; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Required command is unavailable: $command" >&2
        exit 2
    fi
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd "$script_dir/../.." && pwd)"
downloads="$work_root/downloads"
sources="$work_root/sources"
wheelhouse="$work_root/wheelhouse/$abi"
mkdir -p "$downloads" "$sources" "$wheelhouse" "$(dirname "$output_prefix")"

fetch_and_verify() {
    local url="$1"
    local sha256="$2"
    local destination="$3"
    if [[ ! -f "$destination" ]]; then
        curl -Lf --retry 5 --retry-all-errors --output "$destination" "$url"
    fi
    echo "$sha256  $destination" | sha256sum --check --status || {
        echo "Downloaded file does not match its pinned SHA-256: $destination" >&2
        exit 1
    }
}

python_archive="$downloads/Python-${PYTHON_VERSION}.tar.xz"
numpy_archive="$downloads/numpy-${NUMPY_VERSION}.tar.gz"
fetch_and_verify "$PYTHON_URL" "$PYTHON_SHA256" "$python_archive"
fetch_and_verify "$NUMPY_URL" "$NUMPY_SHA256" "$numpy_archive"

python_source="$sources/Python-${PYTHON_VERSION}"
if [[ ! -f "$python_source/Android/android.py" ]]; then
    tar -xf "$python_archive" -C "$sources"
fi
numpy_source="$sources/numpy-${NUMPY_VERSION}"
if [[ ! -f "$numpy_source/pyproject.toml" ]]; then
    tar -xf "$numpy_archive" -C "$sources"
fi
cp "$script_dir/android_numpy_cross.ini" \
    "$numpy_source/.infernux-android-cross.ini"

export ANDROID_HOME
python_prefix="$python_source/cross-build/$host/prefix"
if [[ ! -f "$python_prefix/lib/libpython3.13.so" ]]; then
    "$python_source/Android/android.py" build "$host"
fi
build_python="$python_source/cross-build/build/python"
if [[ ! -x "$build_python" ]]; then
    echo "CPython host build did not produce an executable build Python." >&2
    exit 1
fi

cibw_environment="$work_root/cibuildwheel-${CIBUILDWHEEL_VERSION}"
if [[ ! -x "$cibw_environment/bin/python" ]]; then
    "$build_python" -m venv "$cibw_environment"
    "$cibw_environment/bin/python" -m pip install \
        --disable-pip-version-check "cibuildwheel==${CIBUILDWHEEL_VERSION}"
fi

rm -f "$wheelhouse"/*.whl
(
    cd "$numpy_source"
    ANDROID_API_LEVEL="$minimum_api" \
    CIBW_BUILD="cp313-android_${cibw_arch}" \
    CIBW_TEST_SKIP="*" \
    CIBW_BEFORE_BUILD="" \
    CIBW_CONFIG_SETTINGS="setup-args=--cross-file={project}/.infernux-android-cross.ini setup-args=-Dallow-noblas=true setup-args=-Dblas=none setup-args=-Dlapack=none build-dir=build-infernux-${abi}" \
        "$cibw_environment/bin/python" -m cibuildwheel \
        --platform android \
        --archs "$cibw_arch" \
        --output-dir "$wheelhouse" \
        .
)
numpy_wheel="$(find "$wheelhouse" -maxdepth 1 -type f -name "numpy-${NUMPY_VERSION}-cp313-cp313-android_*_${cibw_arch}.whl" -print -quit)"
if [[ -z "$numpy_wheel" ]]; then
    echo "NumPy build did not produce the expected Android wheel." >&2
    exit 1
fi

staging="$(mktemp -d "$(dirname "$output_prefix")/.android-python-${abi}.XXXXXX")"
cleanup() {
    rm -rf -- "$staging"
}
trap cleanup EXIT
mkdir -p "$staging/include" "$staging/lib" "$staging/wheels"
cp -a "$python_prefix/include/python3.13" "$staging/include/"
cp -a "$python_prefix/lib/python3.13" "$staging/lib/"
cp -a "$python_prefix/lib/libpython3.13.so" "$staging/lib/"
find "$python_prefix/lib" -maxdepth 1 -type f -name "lib*_python.so" \
    -exec cp -a {} "$staging/lib/" \;
cp -a "$numpy_wheel" "$staging/wheels/"

find "$staging" -type d \
    \( -name __pycache__ -o -name test -o -name tests -o -name idlelib \
       -o -name tkinter -o -name turtledemo -o -name ensurepip \) \
    -prune -exec rm -rf -- {} +
find "$staging" -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete

"$build_python" "$script_dir/android_python_runtime.py" stamp "$staging" \
    --abi "$abi" \
    --python-version "$PYTHON_VERSION" \
    --cpython-api "$CPYTHON_ANDROID_API" \
    --minimum-api "$minimum_api" \
    --ndk-version "$CPYTHON_NDK_VERSION" \
    --source-url "$PYTHON_URL" \
    --source-sha256 "$PYTHON_SHA256" >/dev/null
"$build_python" "$script_dir/android_python_runtime.py" verify "$staging" \
    --abi "$abi" \
    --application-minimum-api 26 >/dev/null

mv "$staging" "$output_prefix"
trap - EXIT
echo "Android CPython runtime ready: $output_prefix"
