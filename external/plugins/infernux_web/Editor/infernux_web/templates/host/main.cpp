#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <webgpu/webgpu_cpp.h>

#include <function/renderer/rhi/RhiDescriptors.h>

#include <core/threading/JobSystem.h>
#include <function/audio/AudioEngine.h>
#include <function/renderer/FullscreenRenderer.h>
#include <platform/input/InputManager.h>
#if defined(INFERNUX_WEB_ENGINE_RUNTIME)
#include <function/scene/SceneManager.h>
#include <function/scene/physics/PhysicsWorld.h>
#endif

#include "InfernuxWebHostModule.h"
#include "WebGpuRhiDevice.h"
#include "WebParticleRuntime.h"
#include "WebPostProcessRenderer.h"
#include "WebSceneRenderer.h"
#include "WebScreenUIRenderer.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <exception>
#include <limits>
#include <memory>
#include <string_view>
#include <unordered_map>

#include <emscripten.h>
#include <emscripten/html5.h>

#if defined(INFERNUX_WEB_ENGINE_RUNTIME)
PyMODINIT_FUNC PyInit__Infernux();
#endif

namespace
{

wgpu::Instance g_instance = wgpuCreateInstance(nullptr);
wgpu::Adapter g_adapter;
wgpu::Device g_device;
wgpu::Queue g_queue;
wgpu::Surface g_surface;
std::unique_ptr<infernux::web::WebGpuRhiDevice> g_rhi;
infernux::FullscreenRenderer g_fullscreenRenderer;
infernux::FullscreenPipelineKey g_fullscreenPipelineKey;
infernux::web::WebSceneRenderer g_sceneRenderer;
infernux::web::WebParticleRuntime g_particleRuntime;
infernux::web::WebPostProcessRenderer g_postProcessRenderer;
infernux::web::WebScreenUIRenderer g_screenUIRenderer;
bool g_particleRuntimeReady = false;
bool g_webGpuValidationFailed = false;
wgpu::TextureFormat g_surfaceFormat = wgpu::TextureFormat::Undefined;
PyObject *g_tick = nullptr;
PyObject *g_input = nullptr;
PyObject *g_activate = nullptr;
PyObject *g_runtimeDiagnostic = nullptr;
uint32_t g_width = 1;
uint32_t g_height = 1;
double g_cssWidth = 1.0;
double g_cssHeight = 1.0;
std::array<double, 32> g_gamepadTimestamps{};
std::unordered_map<int, std::pair<float, float>> g_pointerPositions;
double g_lastFrameTimeMilliseconds = 0.0;
bool g_runtimeFrameFailed = false;
double g_safeAreaLeft = 0.0;
double g_safeAreaTop = 0.0;
double g_safeAreaRight = 0.0;
double g_safeAreaBottom = 0.0;
double g_keyboardInset = 0.0;
bool g_keyboardInsetKnown = false;
bool g_runtimeActivated = false;

int BrowserCodeToScancode(std::string_view code)
{
    if (code.size() == 4 && code.substr(0, 3) == "Key" && code[3] >= 'A' && code[3] <= 'Z')
        return 4 + (code[3] - 'A');
    if (code.size() == 6 && code.substr(0, 5) == "Digit" && code[5] >= '1' && code[5] <= '9')
        return 30 + (code[5] - '1');
    if (code == "Digit0")
        return 39;
    if (code == "Enter")
        return 40;
    if (code == "Escape")
        return 41;
    if (code == "Backspace")
        return 42;
    if (code == "Tab")
        return 43;
    if (code == "Space")
        return 44;
    if (code == "ArrowRight")
        return 79;
    if (code == "ArrowLeft")
        return 80;
    if (code == "ArrowDown")
        return 81;
    if (code == "ArrowUp")
        return 82;
    return -1;
}

int BrowserButtonToUnityButton(unsigned short button)
{
    switch (button) {
    case 0:
        return 0;
    case 2:
        return 1;
    case 1:
        return 2;
    case 3:
        return 3;
    case 4:
        return 4;
    default:
        return -1;
    }
}

class WebFullscreenRendererHost final : public infernux::FullscreenRendererHost
{
  public:
    explicit WebFullscreenRendererHost(infernux::web::WebGpuRhiDevice &device) : m_device(device)
    {
    }

    [[nodiscard]] infernux::rhi::Device &GetRhiDevice() noexcept override
    {
        return m_device;
    }

    [[nodiscard]] uint32_t GetFrameCount() const noexcept override
    {
        return 1;
    }

    [[nodiscard]] uint32_t GetCurrentFrame() const noexcept override
    {
        return 0;
    }

    [[nodiscard]] infernux::rhi::ShaderModuleHandle AcquireShaderModule(const std::string &name,
                                                                        infernux::rhi::ShaderStage stage) override
    {
        const char *stageName = nullptr;
        if (stage == infernux::rhi::ShaderStage::Vertex)
            stageName = "vertex";
        else if (stage == infernux::rhi::ShaderStage::Fragment)
            stageName = "fragment";
        else if (stage == infernux::rhi::ShaderStage::Compute)
            stageName = "compute";
        if (stageName == nullptr)
            return {};
        std::string source;
        if (!InfernuxWebFindShaderSource(name, stageName, source))
            return {};
        return m_device.CreateShaderModule(infernux::rhi::ShaderModuleDesc::FromWgsl(source.data(), source.size()));
    }

    [[nodiscard]] infernux::rhi::BindingLayoutHandle GetPerViewLayout() const noexcept override
    {
        return {};
    }

    [[nodiscard]] infernux::rhi::BindingLayoutHandle GetGlobalsLayout() const noexcept override
    {
        return {};
    }

    [[nodiscard]] infernux::rhi::BindGroupHandle GetCurrentGlobalsGroup() override
    {
        return {};
    }

    void ReportError(const std::string &message) override
    {
        std::fprintf(stderr, "INFERNUX_WEB_RENDER_ERROR %s\n", message.c_str());
    }

    void ReportInfo(const std::string &message) override
    {
        std::printf("INFERNUX_WEB_RENDER_INFO %s\n", message.c_str());
    }

  private:
    infernux::web::WebGpuRhiDevice &m_device;
};

void PrintPythonError(const char *context)
{
    std::fprintf(stderr, "INFERNUX_WEB_PYTHON_ERROR context=%s\n", context);
    PyErr_Print();
}

bool VerifyPreloadedRuntime()
{
    static constexpr char pythonArchive[] = "/usr/local/lib/python313.zip";
    std::FILE *stream = std::fopen(pythonArchive, "rb");
    if (stream == nullptr) {
        std::fprintf(stderr, "INFERNUX_WEB_PYTHON_ARCHIVE_MISSING path=%s\n", pythonArchive);
        return false;
    }
    const int seekResult = std::fseek(stream, 0, SEEK_END);
    const long byteSize = seekResult == 0 ? std::ftell(stream) : -1;
    std::array<unsigned char, 4> signature{};
    const bool hasSignature = std::fseek(stream, 0, SEEK_SET) == 0 &&
                              std::fread(signature.data(), 1, signature.size(), stream) == signature.size();
    std::fclose(stream);
    const bool isZip =
        hasSignature && signature[0] == 'P' && signature[1] == 'K' && signature[2] == 3 && signature[3] == 4;
    if (byteSize <= 0 || !isZip) {
        std::fprintf(stderr, "INFERNUX_WEB_PYTHON_ARCHIVE_INVALID bytes=%ld zip=%d\n", byteSize, isZip ? 1 : 0);
        return false;
    }
    std::printf("INFERNUX_WEB_PYTHON_ARCHIVE_READY bytes=%ld\n", byteSize);
    return true;
}

void SetDictString(PyObject *dictionary, const char *key, const char *value)
{
    PyObject *item = PyUnicode_FromString(value != nullptr ? value : "");
    if (item != nullptr) {
        PyDict_SetItemString(dictionary, key, item);
        Py_DECREF(item);
    }
}

void SetDictNumber(PyObject *dictionary, const char *key, double value)
{
    PyObject *item = PyFloat_FromDouble(value);
    if (item != nullptr) {
        PyDict_SetItemString(dictionary, key, item);
        Py_DECREF(item);
    }
}

void SetDictInteger(PyObject *dictionary, const char *key, long value)
{
    PyObject *item = PyLong_FromLong(value);
    if (item != nullptr) {
        PyDict_SetItemString(dictionary, key, item);
        Py_DECREF(item);
    }
}

void SetDictBool(PyObject *dictionary, const char *key, bool value)
{
    PyObject *item = PyBool_FromLong(value ? 1 : 0);
    if (item != nullptr) {
        PyDict_SetItemString(dictionary, key, item);
        Py_DECREF(item);
    }
}

bool ReadSettingFloat(PyObject *settings, const char *key, float &value)
{
    PyObject *item = settings && PyDict_Check(settings) ? PyDict_GetItemString(settings, key) : nullptr;
    if (!item)
        return false;
    const double decoded = PyFloat_AsDouble(item);
    if (PyErr_Occurred() || !std::isfinite(decoded)) {
        PyErr_Clear();
        return false;
    }
    value = static_cast<float>(decoded);
    return true;
}

bool ReadSettingUInt(PyObject *settings, const char *key, uint32_t &value)
{
    PyObject *item = settings && PyDict_Check(settings) ? PyDict_GetItemString(settings, key) : nullptr;
    if (!item || !PyLong_Check(item))
        return false;
    const unsigned long decoded = PyLong_AsUnsignedLong(item);
    if (PyErr_Occurred() || decoded > std::numeric_limits<uint32_t>::max()) {
        PyErr_Clear();
        return false;
    }
    value = static_cast<uint32_t>(decoded);
    return true;
}

bool ConfigurePostProcess(PyObject *settings)
{
    if (!settings || !PyDict_Check(settings))
        return false;
    infernux::web::WebPostProcessRenderer::Settings values;
    PyObject *bloomEnabled = PyDict_GetItemString(settings, "bloom_enabled");
    const int enabled = bloomEnabled ? PyObject_IsTrue(bloomEnabled) : -1;
    if (enabled < 0 || !ReadSettingFloat(settings, "bloom_threshold", values.bloomThreshold) ||
        !ReadSettingFloat(settings, "bloom_intensity", values.bloomIntensity) ||
        !ReadSettingFloat(settings, "bloom_scatter", values.bloomScatter) ||
        !ReadSettingFloat(settings, "bloom_clamp", values.bloomClamp) ||
        !ReadSettingUInt(settings, "bloom_iterations", values.bloomIterations) ||
        !ReadSettingUInt(settings, "tonemapping_mode", values.toneMappingMode) ||
        !ReadSettingFloat(settings, "tonemapping_exposure", values.exposure))
        return false;
    values.bloomEnabled = enabled != 0;
    PyObject *tint = PyDict_GetItemString(settings, "bloom_tint");
    PyObject *sequence = tint ? PySequence_Fast(tint, "bloom_tint must contain three numbers") : nullptr;
    if (!sequence || PySequence_Fast_GET_SIZE(sequence) < 3) {
        Py_XDECREF(sequence);
        PyErr_Clear();
        return false;
    }
    for (Py_ssize_t index = 0; index < 3; ++index) {
        const double component = PyFloat_AsDouble(PySequence_Fast_GET_ITEM(sequence, index));
        if (PyErr_Occurred() || !std::isfinite(component)) {
            Py_DECREF(sequence);
            PyErr_Clear();
            return false;
        }
        values.bloomTint[static_cast<size_t>(index)] = static_cast<float>(component);
    }
    Py_DECREF(sequence);
    return g_postProcessRenderer.Configure(values);
}

void DispatchInput(const char *kind, PyObject *payload)
{
    if (g_input == nullptr || payload == nullptr)
        return;
    PyObject *result = PyObject_CallFunction(g_input, "sO", kind, payload);
    if (result == nullptr)
        PrintPythonError("input");
    else
        Py_DECREF(result);
}

extern "C" EMSCRIPTEN_KEEPALIVE void InfernuxWebUserActivation()
{
    if (g_runtimeActivated || g_activate == nullptr)
        return;

    const bool audioReady = infernux::AudioEngine::Instance().Initialize();
    if (!audioReady) {
        std::fprintf(stderr, "INFERNUX_WEB_AUDIO_INITIALIZATION_FAILED\n");
        return;
    }

    PyObject *argument = PyBool_FromLong(1);
    PyObject *result = argument != nullptr ? PyObject_CallOneArg(g_activate, argument) : nullptr;
    Py_XDECREF(argument);
    if (result == nullptr) {
        PrintPythonError("activation");
        return;
    }
    const int activated = PyObject_IsTrue(result);
    Py_DECREF(result);
    if (activated < 0) {
        PrintPythonError("activation-result");
        return;
    }
    if (activated == 0)
        return;

    g_runtimeActivated = true;
    std::printf("INFERNUX_WEB_AUDIO_READY sample_rate=%d channels=%d\n",
                infernux::AudioEngine::Instance().GetSampleRate(), infernux::AudioEngine::Instance().GetChannelCount());
    std::printf("INFERNUX_WEB_AUDIO_ACTIVE_VOICES count=%zu\n",
                infernux::AudioEngine::Instance().GetActiveVoiceCount());
}

void ResizeCanvas()
{
    double cssWidth = 0.0;
    double cssHeight = 0.0;
    emscripten_get_element_css_size("#canvas", &cssWidth, &cssHeight);
    g_cssWidth = std::max(1.0, cssWidth);
    g_cssHeight = std::max(1.0, cssHeight);
#if INFERNUX_WEB_FIXED_CANVAS
    g_width = static_cast<uint32_t>(INFERNUX_WEB_CANVAS_WIDTH);
    g_height = static_cast<uint32_t>(INFERNUX_WEB_CANVAS_HEIGHT);
    const double scale = static_cast<double>(g_width) / g_cssWidth;
#else
    const double scale = std::max(1.0, emscripten_get_device_pixel_ratio());
    g_width = std::max(1u, static_cast<uint32_t>(cssWidth * scale));
    g_height = std::max(1u, static_cast<uint32_t>(cssHeight * scale));
#endif
    emscripten_set_canvas_element_size("#canvas", g_width, g_height);

    if (g_surface && g_device) {
        wgpu::SurfaceConfiguration config;
        config.device = g_device;
        config.format = g_surfaceFormat;
        config.usage = wgpu::TextureUsage::RenderAttachment;
        config.width = g_width;
        config.height = g_height;
        config.alphaMode = wgpu::CompositeAlphaMode::Auto;
        config.presentMode = wgpu::PresentMode::Fifo;
        g_surface.Configure(&config);
        g_sceneRenderer.Resize(g_width, g_height);
        if (!g_postProcessRenderer.Resize(g_width, g_height))
            std::fprintf(stderr, "INFERNUX_WEB_POST_PROCESS_RESIZE_FAILED width=%u height=%u\n", g_width, g_height);
    }

    PyObject *payload = PyDict_New();
    SetDictInteger(payload, "width", g_width);
    SetDictInteger(payload, "height", g_height);
    SetDictNumber(payload, "pixel_ratio", scale);
    SetDictNumber(payload, "safe_left", g_safeAreaLeft);
    SetDictNumber(payload, "safe_top", g_safeAreaTop);
    SetDictNumber(payload, "safe_right", g_safeAreaRight);
    SetDictNumber(payload, "safe_bottom", g_safeAreaBottom);
    SetDictBool(payload, "keyboard_inset_known", g_keyboardInsetKnown);
    SetDictNumber(payload, "keyboard_inset", g_keyboardInset);
    DispatchInput("viewport", payload);
    Py_XDECREF(payload);
    infernux::InputManager::Instance().ProcessScreenMetrics(
        static_cast<int>(g_cssWidth), static_cast<int>(g_cssHeight), static_cast<int>(g_width),
        static_cast<int>(g_height), static_cast<float>(scale), static_cast<int>(g_safeAreaLeft),
        static_cast<int>(g_safeAreaTop), std::max(0, static_cast<int>(g_cssWidth - g_safeAreaLeft - g_safeAreaRight)),
        std::max(0, static_cast<int>(g_cssHeight - g_safeAreaTop - g_safeAreaBottom)), g_keyboardInsetKnown,
        static_cast<int>(g_keyboardInset));
}

extern "C" EMSCRIPTEN_KEEPALIVE void InfernuxWebResizeCanvas()
{
    ResizeCanvas();
}

extern "C" EMSCRIPTEN_KEEPALIVE void InfernuxWebSetRenderDiagnostic(int feature, int enabled)
{
    switch (feature) {
    case 0:
        g_sceneRenderer.SetSkyEnabledForDiagnostics(enabled != 0);
        break;
    case 1:
        g_sceneRenderer.SetShadowsEnabledForDiagnostics(enabled != 0);
        break;
    default:
        std::fprintf(stderr, "INFERNUX_WEB_RENDER_DIAGNOSTIC_INVALID feature=%d\n", feature);
        break;
    }
}

extern "C" EMSCRIPTEN_KEEPALIVE void InfernuxWebViewportChanged(double safeLeft, double safeTop, double safeRight,
                                                                double safeBottom, int keyboardInsetKnown,
                                                                double keyboardInset)
{
    g_safeAreaLeft = std::max(0.0, safeLeft);
    g_safeAreaTop = std::max(0.0, safeTop);
    g_safeAreaRight = std::max(0.0, safeRight);
    g_safeAreaBottom = std::max(0.0, safeBottom);
    g_keyboardInsetKnown = keyboardInsetKnown != 0;
    g_keyboardInset = g_keyboardInsetKnown ? std::max(0.0, keyboardInset) : 0.0;
    ResizeCanvas();
}

extern "C" EMSCRIPTEN_KEEPALIVE void InfernuxWebPageLifecycle(int active)
{
    auto &input = infernux::InputManager::Instance();
    input.ProcessFocusEvent(active != 0);
    if (!active) {
        g_pointerPositions.clear();
        InfernuxWebEndTextInput();
        infernux::AudioEngine::Instance().PauseAll();
    } else if (g_runtimeActivated) {
        infernux::AudioEngine::Instance().ResumeAll();
    }
    PyObject *payload = PyDict_New();
    SetDictBool(payload, "active", active != 0);
    DispatchInput(active != 0 ? "page_show" : "page_hide", payload);
    Py_DECREF(payload);
}

EM_BOOL OnResize(int, const EmscriptenUiEvent *, void *)
{
    ResizeCanvas();
    return EM_TRUE;
}

EM_BOOL OnFocus(int eventType, const EmscriptenFocusEvent *, void *)
{
    infernux::InputManager::Instance().ProcessFocusEvent(eventType == EMSCRIPTEN_EVENT_FOCUS);
    if (eventType != EMSCRIPTEN_EVENT_FOCUS) {
        g_pointerPositions.clear();
        InfernuxWebEndTextInput();
    }
    PyObject *payload = PyDict_New();
    SetDictBool(payload, "focused", eventType == EMSCRIPTEN_EVENT_FOCUS);
    DispatchInput(eventType == EMSCRIPTEN_EVENT_FOCUS ? "focus" : "blur", payload);
    Py_DECREF(payload);
    return EM_FALSE;
}

EM_BOOL OnVisibility(int, const EmscriptenVisibilityChangeEvent *event, void *)
{
    if (event->hidden) {
        infernux::InputManager::Instance().ProcessFocusEvent(false);
        g_pointerPositions.clear();
        InfernuxWebEndTextInput();
        infernux::AudioEngine::Instance().PauseAll();
    } else if (g_runtimeActivated) {
        infernux::AudioEngine::Instance().ResumeAll();
    }
    PyObject *payload = PyDict_New();
    SetDictBool(payload, "hidden", event->hidden != 0);
    SetDictInteger(payload, "state", event->visibilityState);
    DispatchInput("visibility", payload);
    Py_DECREF(payload);
    return EM_FALSE;
}

EM_BOOL OnKey(int eventType, const EmscriptenKeyboardEvent *event, void *)
{
    if (eventType == EMSCRIPTEN_EVENT_KEYDOWN && !event->repeat)
        InfernuxWebUserActivation();
    infernux::InputManager::Instance().ProcessKeyEvent(BrowserCodeToScancode(event->code),
                                                       eventType == EMSCRIPTEN_EVENT_KEYDOWN);
    PyObject *payload = PyDict_New();
    SetDictString(payload, "key", event->key);
    SetDictString(payload, "code", event->code);
    SetDictBool(payload, "repeat", event->repeat != 0);
    SetDictBool(payload, "ctrl", event->ctrlKey != 0);
    SetDictBool(payload, "shift", event->shiftKey != 0);
    SetDictBool(payload, "alt", event->altKey != 0);
    SetDictBool(payload, "meta", event->metaKey != 0);
    DispatchInput(eventType == EMSCRIPTEN_EVENT_KEYDOWN ? "key_down" : "key_up", payload);
    Py_XDECREF(payload);
    return EM_TRUE;
}

extern "C" EMSCRIPTEN_KEEPALIVE int InfernuxWebGetKeyState(int scancode)
{
    return infernux::InputManager::Instance().GetKey(scancode) ? 1 : 0;
}

extern "C" EMSCRIPTEN_KEEPALIVE double InfernuxWebGetObjectPositionAxis(const char *name, int axis)
{
#if defined(INFERNUX_WEB_ENGINE_RUNTIME)
    if (name == nullptr || axis < 0 || axis > 2)
        return std::numeric_limits<double>::quiet_NaN();

    auto readScene = [&](infernux::Scene *scene) -> double {
        if (scene == nullptr)
            return std::numeric_limits<double>::quiet_NaN();
        for (infernux::GameObject *object : scene->GetAllObjects()) {
            if (object == nullptr || object->GetName() != name)
                continue;
            const glm::vec3 position = object->GetTransform()->GetWorldPosition();
            return static_cast<double>(position[axis]);
        }
        return std::numeric_limits<double>::quiet_NaN();
    };

    auto &sceneManager = infernux::SceneManager::Instance();
    const double activeValue = readScene(sceneManager.GetActiveScene());
    if (!std::isnan(activeValue))
        return activeValue;
    return readScene(sceneManager.GetRuntimePersistentScene());
#else
    (void)name;
    (void)axis;
    return std::numeric_limits<double>::quiet_NaN();
#endif
}

extern "C" EMSCRIPTEN_KEEPALIVE double InfernuxWebGetRuntimeDiagnostic(int probe, int argument)
{
    if (g_runtimeDiagnostic == nullptr)
        return std::numeric_limits<double>::quiet_NaN();
    PyObject *result = PyObject_CallFunction(g_runtimeDiagnostic, "ii", probe, argument);
    if (result == nullptr) {
        PrintPythonError("runtime-diagnostic");
        return std::numeric_limits<double>::quiet_NaN();
    }
    const double value = PyFloat_AsDouble(result);
    Py_DECREF(result);
    if (PyErr_Occurred()) {
        PrintPythonError("runtime-diagnostic-value");
        return std::numeric_limits<double>::quiet_NaN();
    }
    return value;
}

EM_BOOL OnWheel(int, const EmscriptenWheelEvent *event, void *)
{
    infernux::InputManager::Instance().ProcessScrollEvent(static_cast<float>(event->deltaX),
                                                          static_cast<float>(-event->deltaY));
    PyObject *payload = PyDict_New();
    SetDictNumber(payload, "delta_x", event->deltaX);
    SetDictNumber(payload, "delta_y", event->deltaY);
    SetDictInteger(payload, "delta_mode", event->deltaMode);
    DispatchInput("wheel", payload);
    Py_XDECREF(payload);
    return EM_TRUE;
}

extern "C" EMSCRIPTEN_KEEPALIVE void InfernuxWebPointerEvent(int eventKind, int pointerId, int pointerType,
                                                             double xPixels, double yPixels, double movementXPixels,
                                                             double movementYPixels, double pressure,
                                                             double contactWidthPixels, double contactHeightPixels,
                                                             int button, int buttons, int isPrimary)
{
    if (eventKind == 0)
        InfernuxWebUserActivation();
    auto &input = infernux::InputManager::Instance();
    const float normalizedX = static_cast<float>(xPixels / g_cssWidth);
    const float normalizedY = static_cast<float>(yPixels / g_cssHeight);
    const auto previous = g_pointerPositions.find(pointerId);
    const float deltaX = previous == g_pointerPositions.end() ? 0.0f : normalizedX - previous->second.first;
    const float deltaY = previous == g_pointerPositions.end() ? 0.0f : normalizedY - previous->second.second;
    const bool terminal = eventKind == 2 || eventKind == 3;

    if (pointerType == 0) {
        input.ProcessPointerMotionEvent(static_cast<float>(xPixels), static_cast<float>(yPixels),
                                        static_cast<float>(movementXPixels), static_cast<float>(movementYPixels));
        const int mappedButton = BrowserButtonToUnityButton(static_cast<unsigned short>(button));
        if (mappedButton >= 0 && (eventKind == 0 || terminal))
            input.ProcessPointerButtonEvent(mappedButton, eventKind == 0);
    } else {
        infernux::TouchPhase phase = infernux::TouchPhase::Moved;
        if (eventKind == 0)
            phase = infernux::TouchPhase::Began;
        else if (eventKind == 2)
            phase = infernux::TouchPhase::Ended;
        else if (eventKind == 3)
            phase = infernux::TouchPhase::Canceled;
        input.ProcessTouchEvent(static_cast<uint64_t>(pointerType), static_cast<uint64_t>(pointerId),
                                static_cast<uint64_t>(emscripten_get_now() * 1000000.0), 0, normalizedX, normalizedY,
                                deltaX, deltaY, static_cast<float>(std::clamp(pressure, 0.0, 1.0)), phase,
                                static_cast<float>(contactWidthPixels / g_cssWidth),
                                static_cast<float>(contactHeightPixels / g_cssHeight), isPrimary != 0);
    }

    if (terminal)
        g_pointerPositions.erase(pointerId);
    else
        g_pointerPositions[pointerId] = {normalizedX, normalizedY};

    PyObject *payload = PyDict_New();
    SetDictInteger(payload, "id", pointerId);
    SetDictInteger(payload, "pointer_type", pointerType);
    SetDictNumber(payload, "x", xPixels);
    SetDictNumber(payload, "y", yPixels);
    SetDictNumber(payload, "normalized_x", normalizedX);
    SetDictNumber(payload, "normalized_y", normalizedY);
    SetDictNumber(payload, "movement_x", movementXPixels);
    SetDictNumber(payload, "movement_y", movementYPixels);
    SetDictNumber(payload, "pressure", pressure);
    SetDictNumber(payload, "contact_width", contactWidthPixels);
    SetDictNumber(payload, "contact_height", contactHeightPixels);
    SetDictInteger(payload, "button", button);
    SetDictInteger(payload, "buttons", buttons);
    SetDictBool(payload, "primary", isPrimary != 0);
    static constexpr const char *eventNames[] = {"pointer_down", "pointer_move", "pointer_up", "pointer_cancel"};
    DispatchInput(eventKind >= 0 && eventKind < 4 ? eventNames[eventKind] : "pointer_move", payload);
    Py_DECREF(payload);
}

void PollGamepads()
{
    if (emscripten_sample_gamepad_data() != EMSCRIPTEN_RESULT_SUCCESS)
        return;
    const int count = std::min(emscripten_get_num_gamepads(), static_cast<int>(g_gamepadTimestamps.size()));
    for (int index = 0; index < count; ++index) {
        EmscriptenGamepadEvent event{};
        if (emscripten_get_gamepad_status(index, &event) != EMSCRIPTEN_RESULT_SUCCESS || !event.connected)
            continue;
        if (event.timestamp == g_gamepadTimestamps[static_cast<size_t>(index)])
            continue;
        g_gamepadTimestamps[static_cast<size_t>(index)] = event.timestamp;

        PyObject *payload = PyDict_New();
        SetDictInteger(payload, "index", event.index);
        SetDictString(payload, "id", event.id);
        SetDictString(payload, "mapping", event.mapping);
        PyObject *axes = PyList_New(event.numAxes);
        for (int axis = 0; axis < event.numAxes; ++axis)
            PyList_SetItem(axes, axis, PyFloat_FromDouble(event.axis[axis]));
        PyObject *buttons = PyList_New(event.numButtons);
        for (int button = 0; button < event.numButtons; ++button) {
            PyObject *state = PyDict_New();
            SetDictNumber(state, "value", event.analogButton[button]);
            SetDictBool(state, "pressed", event.digitalButton[button] != 0);
            PyList_SetItem(buttons, button, state);
        }
        PyDict_SetItemString(payload, "axes", axes);
        PyDict_SetItemString(payload, "buttons", buttons);
        Py_DECREF(axes);
        Py_DECREF(buttons);
        DispatchInput("gamepad", payload);
        Py_DECREF(payload);
    }
}

infernux::rhi::PixelFormat ToRhiFormat(wgpu::TextureFormat format)
{
    switch (format) {
    case wgpu::TextureFormat::RGBA8Unorm:
        return infernux::rhi::PixelFormat::RGBA8UNorm;
    case wgpu::TextureFormat::RGBA8UnormSrgb:
        return infernux::rhi::PixelFormat::RGBA8Srgb;
    case wgpu::TextureFormat::BGRA8Unorm:
        return infernux::rhi::PixelFormat::BGRA8UNorm;
    case wgpu::TextureFormat::BGRA8UnormSrgb:
        return infernux::rhi::PixelFormat::BGRA8Srgb;
    default:
        return infernux::rhi::PixelFormat::Undefined;
    }
}

bool CreateRhiPipeline()
{
    if (ToRhiFormat(g_surfaceFormat) == infernux::rhi::PixelFormat::Undefined)
        return false;

    g_rhi = std::make_unique<infernux::web::WebGpuRhiDevice>(g_device, g_queue);
    g_fullscreenRenderer.Initialize(std::make_shared<WebFullscreenRendererHost>(*g_rhi));
    g_fullscreenPipelineKey.shaderName = "Web Host";
    g_fullscreenPipelineKey.colorFormat = infernux::rhi::PixelFormat::RGBA16SFloat;
    g_fullscreenPipelineKey.useDynamicRendering = true;
    return g_fullscreenRenderer.EnsurePipeline(g_fullscreenPipelineKey).pipeline.IsValid();
}

extern "C" EMSCRIPTEN_KEEPALIVE void InfernuxWebTextInput(const char *text)
{
    infernux::InputManager::Instance().ProcessTextInputEvent(text != nullptr ? text : "");
    PyObject *payload = PyDict_New();
    SetDictString(payload, "text", text);
    DispatchInput("text_input", payload);
    Py_DECREF(payload);
}

bool InitializePython()
{
    if (!VerifyPreloadedRuntime())
        return false;
#if defined(INFERNUX_WEB_ENGINE_RUNTIME)
    if (!infernux::JobSystem::IsAvailable())
        infernux::JobSystem::InitializeInline();
#endif
    if (PyImport_AppendInittab("_InfernuxWebHost", &PyInit__InfernuxWebHost) == -1) {
        std::fprintf(stderr, "INFERNUX_WEB_HOST_MODULE_REGISTRATION_FAILED\n");
        return false;
    }
#if defined(INFERNUX_WEB_ENGINE_RUNTIME)
    if (PyImport_AppendInittab("_Infernux", &PyInit__Infernux) == -1) {
        std::fprintf(stderr, "INFERNUX_WEB_NATIVE_MODULE_REGISTRATION_FAILED\n");
        return false;
    }
#endif
    Py_Initialize();
    if (!Py_IsInitialized())
        return false;
#if defined(INFERNUX_WEB_ENGINE_RUNTIME)
    if (PyRun_SimpleString("import _Infernux\n"
                           "assert _Infernux.__runtime_profile__ == 'web-player'\n"
                           "print('INFERNUX_WEB_NATIVE_MODULE_READY profile=web-player')") != 0) {
        PrintPythonError("native-module");
        return false;
    }
#endif
    if (PyRun_SimpleString("exec(open('/infernux/bootstrap.py', encoding='utf-8').read(), globals())") != 0) {
        PrintPythonError("bootstrap");
        return false;
    }
    PyObject *mainModule = PyImport_AddModule("__main__");
#if defined(INFERNUX_WEB_ENGINE_RUNTIME)
    PyObject *configurePhysics = PyObject_GetAttrString(mainModule, "infernux_web_configure_physics");
    if (configurePhysics == nullptr || !PyCallable_Check(configurePhysics)) {
        Py_XDECREF(configurePhysics);
        PrintPythonError("physics-configuration-contract");
        return false;
    }
    PyObject *physicsConfiguration = PyObject_CallNoArgs(configurePhysics);
    Py_DECREF(configurePhysics);
    if (physicsConfiguration == nullptr) {
        PrintPythonError("physics-configuration");
        return false;
    }
    const int physicsConfigured = PyObject_IsTrue(physicsConfiguration);
    Py_DECREF(physicsConfiguration);
    if (physicsConfigured != 1) {
        std::fprintf(stderr, "INFERNUX_WEB_PHYSICS_CONFIGURATION_FAILED\n");
        return false;
    }
    try {
        // The Web host does not construct the desktop Infernux application.
        // Bootstrap has extracted and applied project settings, but it has not
        // loaded the scene yet, so Collider creation still happens after Jolt.
        infernux::PhysicsWorld::Instance().Initialize();
    } catch (const std::exception &error) {
        std::fprintf(stderr, "INFERNUX_WEB_PHYSICS_INITIALIZATION_FAILED %s\n", error.what());
        return false;
    }
    std::printf("INFERNUX_WEB_PHYSICS_READY\n");
#endif
    g_tick = PyObject_GetAttrString(mainModule, "infernux_web_tick");
    g_input = PyObject_GetAttrString(mainModule, "infernux_web_input");
    g_activate = PyObject_GetAttrString(mainModule, "infernux_web_activate");
    g_runtimeDiagnostic = PyObject_GetAttrString(mainModule, "infernux_web_runtime_diagnostic");
    return g_tick != nullptr && PyCallable_Check(g_tick) && g_input != nullptr && PyCallable_Check(g_input) &&
           g_activate != nullptr && PyCallable_Check(g_activate) && g_runtimeDiagnostic != nullptr &&
           PyCallable_Check(g_runtimeDiagnostic);
}

void Frame()
{
    const double frameTimeMilliseconds = emscripten_get_now();
    const double deltaSeconds =
        g_lastFrameTimeMilliseconds > 0.0 ? (frameTimeMilliseconds - g_lastFrameTimeMilliseconds) / 1000.0 : 0.0;
    g_lastFrameTimeMilliseconds = frameTimeMilliseconds;
#if defined(INFERNUX_WEB_ENGINE_RUNTIME)
    if (infernux::JobSystem::IsAvailable())
        infernux::JobSystem::Get().RunPendingJobs(64);
#endif
    PollGamepads();
    if (g_tick != nullptr && !g_runtimeFrameFailed) {
        PyObject *result = PyObject_CallFunction(g_tick, "d", deltaSeconds);
        if (result == nullptr) {
            PrintPythonError("frame");
            g_runtimeFrameFailed = true;
        } else
            Py_DECREF(result);
    }
#if defined(INFERNUX_WEB_ENGINE_RUNTIME)
    if (!g_runtimeFrameFailed) {
        auto &sceneManager = infernux::SceneManager::Instance();
        const float delta = static_cast<float>(std::clamp(deltaSeconds, 0.0, 0.25));
        sceneManager.Update(delta);
        sceneManager.LateUpdate(delta);
        sceneManager.EndFrame();
    }
#endif
    wgpu::SurfaceTexture surfaceTexture;
    g_surface.GetCurrentTexture(&surfaceTexture);
    if (surfaceTexture.texture) {
        wgpu::TextureView view = surfaceTexture.texture.CreateView();
        wgpu::RenderPassColorAttachment colorAttachment;
        colorAttachment.view = g_postProcessRenderer.SceneColorView();
        colorAttachment.loadOp = wgpu::LoadOp::Clear;
        colorAttachment.storeOp = wgpu::StoreOp::Store;
        colorAttachment.clearValue = {0.015, 0.035, 0.065, 1.0};
        wgpu::RenderPassDescriptor passDescriptor;
        passDescriptor.colorAttachmentCount = 1;
        passDescriptor.colorAttachments = &colorAttachment;
        wgpu::RenderPassDepthStencilAttachment depthAttachment;
        if (g_sceneRenderer.HasDepthTarget()) {
            depthAttachment.view = g_sceneRenderer.GetDepthView();
            depthAttachment.depthLoadOp = wgpu::LoadOp::Clear;
            depthAttachment.depthStoreOp = wgpu::StoreOp::Store;
            depthAttachment.depthClearValue = 1.0f;
        }
        wgpu::CommandEncoder encoder = g_device.CreateCommandEncoder();
        if (g_particleRuntimeReady && !g_webGpuValidationFailed)
            g_particleRuntime.RecordCompute(encoder);
        const bool scenePrepared = !g_webGpuValidationFailed && g_sceneRenderer.Prepare(encoder, g_width, g_height);
        if (scenePrepared && g_sceneRenderer.HasDepthTarget())
            passDescriptor.depthStencilAttachment = &depthAttachment;
        wgpu::RenderPassEncoder pass = encoder.BeginRenderPass(&passDescriptor);
        const bool renderedScene = scenePrepared && g_sceneRenderer.RenderPrepared(pass);
        if (!renderedScene && g_rhi) {
            infernux::web::WebGpuGraphicsCommandContext context;
            auto commands = g_rhi->MakeGraphicsCommandEncoder(context, pass);
            const auto &pipeline = g_fullscreenRenderer.EnsurePipeline(g_fullscreenPipelineKey);
            infernux::FullscreenPushConstants pushConstants;
            g_fullscreenRenderer.Draw(commands, pipeline, {}, {}, pushConstants, sizeof(pushConstants));
        }
        if (scenePrepared && g_particleRuntimeReady && !g_webGpuValidationFailed)
            (void)g_particleRuntime.Render(pass, g_width, g_height);
        pass.End();
        if (!g_webGpuValidationFailed && !g_postProcessRenderer.PrepareBloom(encoder)) {
            std::fprintf(stderr, "INFERNUX_WEB_BLOOM_RECORDING_FAILED\n");
            g_webGpuValidationFailed = true;
        }

        wgpu::RenderPassColorAttachment presentAttachment;
        presentAttachment.view = view;
        presentAttachment.loadOp = wgpu::LoadOp::Clear;
        presentAttachment.storeOp = wgpu::StoreOp::Store;
        presentAttachment.clearValue = {0.0, 0.0, 0.0, 1.0};
        wgpu::RenderPassDescriptor presentDescriptor;
        presentDescriptor.colorAttachmentCount = 1;
        presentDescriptor.colorAttachments = &presentAttachment;
        wgpu::RenderPassEncoder presentPass = encoder.BeginRenderPass(&presentDescriptor);
        if (!g_webGpuValidationFailed)
            (void)g_postProcessRenderer.Render(presentPass);
        if (!g_webGpuValidationFailed) {
            (void)g_screenUIRenderer.Render(presentPass, 0, g_width, g_height);
            (void)g_screenUIRenderer.Render(presentPass, 1, g_width, g_height);
        }
        presentPass.End();
        wgpu::CommandBuffer commands = encoder.Finish();
        g_queue.Submit(1, &commands);
    }
    infernux::InputManager::Instance().BeginFrame();
}

void StartSurface()
{
    g_queue = g_device.GetQueue();
    wgpu::EmscriptenSurfaceSourceCanvasHTMLSelector canvasSource;
    canvasSource.selector = "#canvas";
    wgpu::SurfaceDescriptor surfaceDescriptor;
    surfaceDescriptor.nextInChain = &canvasSource;
    g_surface = g_instance.CreateSurface(&surfaceDescriptor);

    wgpu::SurfaceCapabilities capabilities;
    g_surface.GetCapabilities(g_adapter, &capabilities);
    if (capabilities.formatCount == 0) {
        std::fprintf(stderr, "INFERNUX_WEBGPU_NO_SURFACE_FORMAT\n");
        return;
    }
    g_surfaceFormat = capabilities.formats[0];
    if (!CreateRhiPipeline()) {
        std::fprintf(stderr, "INFERNUX_WEBGPU_RHI_PIPELINE_FAILED %s\n",
                     g_rhi ? g_rhi->LastError().c_str() : "unsupported surface format");
        return;
    }
    if (!g_postProcessRenderer.Initialize(g_device, g_surfaceFormat)) {
        std::fprintf(stderr, "INFERNUX_WEB_POST_PROCESS_INITIALIZATION_FAILED\n");
        return;
    }
    const auto sceneColorFormat = g_postProcessRenderer.SceneColorFormat();
    if (!g_sceneRenderer.Initialize(g_device, g_queue, sceneColorFormat)) {
        std::fprintf(stderr, "INFERNUX_WEBGPU_SCENE_PIPELINE_FAILED\n");
        return;
    }
    if (!g_screenUIRenderer.Initialize(g_device, g_queue, g_surfaceFormat)) {
        std::fprintf(stderr, "INFERNUX_WEB_SCREEN_UI_INITIALIZATION_FAILED\n");
        return;
    }
    InfernuxWebSetScreenUIRenderer(&g_screenUIRenderer);
    if (!g_particleRuntime.Initialize(*g_rhi, infernux::rhi::PixelFormat::RGBA16SFloat)) {
        std::fprintf(stderr, "INFERNUX_WEBGPU_PARTICLE_RUNTIME_FAILED %s\n", g_particleRuntime.LastError().c_str());
    } else {
        g_particleRuntimeReady = true;
        InfernuxWebSetParticleRuntime(&g_particleRuntime);
    }
    std::printf("INFERNUX_WEBGPU_FULLSCREEN_RHI_READY\n");
    ResizeCanvas();
    emscripten_set_resize_callback(EMSCRIPTEN_EVENT_TARGET_WINDOW, nullptr, false, OnResize);
    emscripten_set_focus_callback(EMSCRIPTEN_EVENT_TARGET_WINDOW, nullptr, true, OnFocus);
    emscripten_set_blur_callback(EMSCRIPTEN_EVENT_TARGET_WINDOW, nullptr, true, OnFocus);
    emscripten_set_visibilitychange_callback(nullptr, true, OnVisibility);
    emscripten_set_keydown_callback(EMSCRIPTEN_EVENT_TARGET_WINDOW, nullptr, true, OnKey);
    emscripten_set_keyup_callback(EMSCRIPTEN_EVENT_TARGET_WINDOW, nullptr, true, OnKey);
    emscripten_set_wheel_callback("#canvas", nullptr, true, OnWheel);
    PyObject *details = PyDict_New();
    SetDictString(details, "graphics_api", "webgpu");
    SetDictInteger(details, "width", g_width);
    SetDictInteger(details, "height", g_height);
    PyObject *mainModule = PyImport_AddModule("__main__");
    PyObject *ready = PyObject_GetAttrString(mainModule, "infernux_web_ready");
    if (ready != nullptr) {
        PyObject *result = PyObject_CallOneArg(ready, details);
        if (result == nullptr)
            PrintPythonError("ready");
        else {
            if (!ConfigurePostProcess(result)) {
                std::fprintf(stderr, "INFERNUX_WEB_RENDER_STACK_CONFIGURATION_FAILED\n");
                g_webGpuValidationFailed = true;
            }
            Py_DECREF(result);
        }
        Py_DECREF(ready);
    }
    Py_DECREF(details);

    std::printf("INFERNUX_WEBGPU_DEVICE_READY format=%d\n", static_cast<int>(g_surfaceFormat));
    g_lastFrameTimeMilliseconds = emscripten_get_now();
    infernux::InputManager::Instance().BeginFrame();
    emscripten_set_main_loop(Frame, 0, false);
}

} // namespace

int main()
{
    std::printf("INFERNUX_WEB_PLAYER_START\n");
    if (!InitializePython()) {
        std::fprintf(stderr, "INFERNUX_WEB_PYTHON_INITIALIZATION_FAILED\n");
        return 1;
    }
    g_instance.RequestAdapter(
        nullptr, wgpu::CallbackMode::AllowSpontaneous,
        [](wgpu::RequestAdapterStatus status, wgpu::Adapter adapter, wgpu::StringView message) {
            if (status != wgpu::RequestAdapterStatus::Success) {
                std::fprintf(stderr, "INFERNUX_WEBGPU_ADAPTER_FAILED %.*s\n", static_cast<int>(message.length),
                             message.data);
                return;
            }
            g_adapter = adapter;
            wgpu::DeviceDescriptor descriptor;
            wgpu::Limits adapterLimits;
            wgpu::Limits requiredLimits;
            constexpr uint32_t kParticleStorageBufferLimit = 12;
            if (g_adapter.GetLimits(&adapterLimits)) {
                std::printf("INFERNUX_WEBGPU_ADAPTER_LIMITS bind_groups=%u storage_buffers=%u bindings=%u\n",
                            adapterLimits.maxBindGroups, adapterLimits.maxStorageBuffersPerShaderStage,
                            adapterLimits.maxBindingsPerBindGroup);
                if (adapterLimits.maxStorageBuffersPerShaderStage >= kParticleStorageBufferLimit) {
                    requiredLimits.maxStorageBuffersPerShaderStage = kParticleStorageBufferLimit;
                    descriptor.requiredLimits = &requiredLimits;
                } else {
                    std::fprintf(stderr, "INFERNUX_WEBGPU_PARTICLE_LIMIT_UNAVAILABLE required=%u available=%u\n",
                                 kParticleStorageBufferLimit, adapterLimits.maxStorageBuffersPerShaderStage);
                }
            }
            descriptor.SetUncapturedErrorCallback(
                [](const wgpu::Device &, wgpu::ErrorType type, wgpu::StringView errorMessage) {
                    g_webGpuValidationFailed = true;
                    std::fprintf(stderr, "INFERNUX_WEBGPU_UNCAPTURED_ERROR type=%d message=%.*s\n",
                                 static_cast<int>(type), static_cast<int>(errorMessage.length), errorMessage.data);
                });
            descriptor.SetDeviceLostCallback(
                wgpu::CallbackMode::AllowSpontaneous,
                [](const wgpu::Device &, wgpu::DeviceLostReason reason, wgpu::StringView lostMessage) {
                    std::fprintf(stderr, "INFERNUX_WEBGPU_DEVICE_LOST reason=%d message=%.*s\n",
                                 static_cast<int>(reason), static_cast<int>(lostMessage.length), lostMessage.data);
                });
            g_adapter.RequestDevice(
                &descriptor, wgpu::CallbackMode::AllowSpontaneous,
                [](wgpu::RequestDeviceStatus status, wgpu::Device device, wgpu::StringView message) {
                    if (status != wgpu::RequestDeviceStatus::Success) {
                        std::fprintf(stderr, "INFERNUX_WEBGPU_DEVICE_FAILED %.*s\n", static_cast<int>(message.length),
                                     message.data);
                        return;
                    }
                    g_device = device;
                    StartSurface();
                });
        });
    return 0;
}
