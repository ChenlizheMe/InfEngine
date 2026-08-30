#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include "InfernuxWebHostModule.h"
#include "WebParticleRuntime.h"
#include "WebScreenUIRenderer.h"

#include <platform/filesystem/InxPack.h>
#include <platform/input/InputManager.h>

#include <emscripten.h>

#if defined(INFERNUX_WEB_ENGINE_RUNTIME)
#include <function/audio/AudioClipLoader.h>
#include <function/resources/AssetDatabase/AssetDatabase.h>
#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxFileLoader/InxDefaultLoader.hpp>
#include <function/resources/InxFileLoader/InxPythonScriptLoader.hpp>
#include <function/resources/InxMaterial/MaterialLoader.h>
#include <function/resources/InxMesh/MeshLoader.h>
#include <function/resources/InxTexture/TextureLoader.h>
#include <function/resources/PhysicMaterial/PhysicMaterialLoader.h>
#endif

#include <exception>
#include <filesystem>
#include <mutex>
#include <string>
#include <unordered_map>

namespace
{

std::mutex g_shaderMutex;
std::unordered_map<std::string, std::string> g_shaderSources;
infernux::web::WebParticleRuntime *g_particleRuntime = nullptr;
infernux::web::WebScreenUIRenderer *g_screenUIRenderer = nullptr;
bool g_textInputActive = false;

std::string ShaderKey(const std::string &name, const char *stage)
{
    return name + '\0' + (stage != nullptr ? stage : "");
}

PyObject *ReadPackageEntry(PyObject *, PyObject *arguments)
{
    const char *packagePath = nullptr;
    const char *entryPath = nullptr;
    if (!PyArg_ParseTuple(arguments, "ss:read_entry", &packagePath, &entryPath))
        return nullptr;

    try {
        const auto bytes = infernux::inxpack::ReadEntry(std::filesystem::path(packagePath), entryPath);
        return PyBytes_FromStringAndSize(reinterpret_cast<const char *>(bytes.data()),
                                         static_cast<Py_ssize_t>(bytes.size()));
    } catch (const std::exception &error) {
        PyErr_SetString(PyExc_RuntimeError, error.what());
        return nullptr;
    }
}

PyObject *ExtractPackage(PyObject *, PyObject *arguments)
{
    const char *packagePath = nullptr;
    const char *destinationPath = nullptr;
    if (!PyArg_ParseTuple(arguments, "ss:extract_package", &packagePath, &destinationPath))
        return nullptr;

    try {
        const auto manifest =
            infernux::inxpack::Extract(std::filesystem::path(packagePath), std::filesystem::path(destinationPath));
        PyObject *summary = PyDict_New();
        if (summary == nullptr)
            return nullptr;
        const auto setInteger = [summary](const char *name, uint64_t value) {
            PyObject *item = PyLong_FromUnsignedLongLong(value);
            if (item == nullptr)
                return false;
            const int result = PyDict_SetItemString(summary, name, item);
            Py_DECREF(item);
            return result == 0;
        };
        PyObject *hash = PyUnicode_FromString(infernux::inxpack::HashToHex(manifest.archiveHash).c_str());
        const bool complete = setInteger("entries", static_cast<uint64_t>(manifest.entries.size())) &&
                              setInteger("raw_bytes", manifest.rawBytes) &&
                              setInteger("stored_bytes", manifest.storedBytes) &&
                              setInteger("archive_bytes", manifest.archiveBytes) && hash != nullptr &&
                              PyDict_SetItemString(summary, "archive_sha256", hash) == 0;
        Py_XDECREF(hash);
        if (!complete) {
            Py_DECREF(summary);
            return nullptr;
        }
        return summary;
    } catch (const std::exception &error) {
        PyErr_SetString(PyExc_RuntimeError, error.what());
        return nullptr;
    }
}

PyObject *RegisterShader(PyObject *, PyObject *arguments)
{
    const char *name = nullptr;
    const char *stage = nullptr;
    PyObject *sourceObject = nullptr;
    if (!PyArg_ParseTuple(arguments, "ssO:register_shader", &name, &stage, &sourceObject))
        return nullptr;
    if (name == nullptr || *name == '\0' ||
        (std::string(stage) != "vertex" && std::string(stage) != "fragment" && std::string(stage) != "compute")) {
        PyErr_SetString(PyExc_ValueError, "shader name and stage are invalid");
        return nullptr;
    }
    Py_ssize_t sourceSize = 0;
    const char *source = PyUnicode_AsUTF8AndSize(sourceObject, &sourceSize);
    if (source == nullptr)
        return nullptr;
    if (sourceSize <= 0) {
        PyErr_SetString(PyExc_ValueError, "shader source is empty");
        return nullptr;
    }
    {
        std::lock_guard lock(g_shaderMutex);
        const auto [entry, inserted] =
            g_shaderSources.emplace(ShaderKey(name, stage), std::string(source, static_cast<size_t>(sourceSize)));
        if (!inserted) {
            PyErr_SetString(PyExc_ValueError, "shader identity is already registered");
            return nullptr;
        }
    }
    Py_RETURN_NONE;
}

PyObject *InitializeRuntimeAssets(PyObject *, PyObject *arguments)
{
#if !defined(INFERNUX_WEB_ENGINE_RUNTIME)
    PyErr_SetString(PyExc_RuntimeError, "the Web engine runtime is not linked");
    return nullptr;
#else
    const char *projectRoot = nullptr;
    const char *recordsPath = nullptr;
    if (!PyArg_ParseTuple(arguments, "ss:initialize_runtime_assets", &projectRoot, &recordsPath))
        return nullptr;

    try {
        auto &registry = infernux::AssetRegistry::Instance();
        if (registry.IsInitialized())
            throw std::logic_error("the Web runtime asset registry is already initialized");

        auto database = std::make_unique<infernux::AssetDatabase>();
        database->InitializeRuntime(projectRoot);
        database->InstallRuntimeAssetCatalog(recordsPath, true);
        const auto assetCount = database->GetAssetCount();
        registry.Initialize(std::move(database));
        registry.RegisterLoader(infernux::ResourceType::Material, std::make_unique<infernux::MaterialLoader>());
        registry.RegisterLoader(infernux::ResourceType::PhysicMaterial,
                                std::make_unique<infernux::PhysicMaterialLoader>());
        registry.RegisterLoader(infernux::ResourceType::Texture, std::make_unique<infernux::TextureLoader>());
        registry.RegisterLoader(infernux::ResourceType::Mesh, std::make_unique<infernux::MeshLoader>());
        registry.RegisterLoader(infernux::ResourceType::Audio, std::make_unique<infernux::AudioClipLoader>());
        registry.RegisterLoader(infernux::ResourceType::Script, std::make_unique<infernux::InxPythonScriptLoader>());
        registry.RegisterLoader(infernux::ResourceType::DefaultText,
                                std::make_unique<infernux::InxDefaultTextLoader>());
        registry.RegisterLoader(infernux::ResourceType::RenderEffect,
                                std::make_unique<infernux::InxDefaultTextLoader>(infernux::ResourceType::RenderEffect));
        registry.RegisterLoader(infernux::ResourceType::ParticleGraph, std::make_unique<infernux::InxDefaultTextLoader>(
                                                                           infernux::ResourceType::ParticleGraph));
        registry.RegisterLoader(infernux::ResourceType::DefaultBinary,
                                std::make_unique<infernux::InxDefaultBinaryLoader>());
        registry.PopulateAssetDatabaseLoaders();
        return PyLong_FromSize_t(assetCount);
    } catch (const std::exception &error) {
        PyErr_SetString(PyExc_RuntimeError, error.what());
        return nullptr;
    }
#endif
}

PyObject *ReplaceGpuParticleGraph(PyObject *, PyObject *arguments)
{
    unsigned long long graphId = 0;
    PyObject *programs = nullptr;
    PyObject *removeIds = nullptr;
    if (!PyArg_ParseTuple(arguments, "KOO:_replace_gpu_particle_graph", &graphId, &programs, &removeIds))
        return nullptr;
    const std::string error = g_particleRuntime
                                  ? g_particleRuntime->ReplaceGraph(static_cast<uint64_t>(graphId), programs, removeIds)
                                  : "WebGPU particle runtime is not initialized";
    return PyUnicode_FromString(error.c_str());
}

PyObject *ReplaceGpuParticleGraphs(PyObject *, PyObject *arguments)
{
    PyObject *graphs = nullptr;
    if (!PyArg_ParseTuple(arguments, "O:_replace_gpu_particle_graphs", &graphs))
        return nullptr;
    if (!g_particleRuntime)
        return PyUnicode_FromString("WebGPU particle runtime is not initialized");
    PyObject *sequence = PySequence_Fast(graphs, "particle graphs must be a sequence");
    if (!sequence)
        return nullptr;
    std::string error;
    for (Py_ssize_t index = 0; error.empty() && index < PySequence_Fast_GET_SIZE(sequence); ++index) {
        PyObject *graph = PySequence_Fast_GET_ITEM(sequence, index);
        if (!PyDict_Check(graph)) {
            error = "WebGPU particle graph batch entries must be dictionaries";
            break;
        }
        unsigned long long graphId = PyLong_AsUnsignedLongLong(PyDict_GetItemString(graph, "graph_instance_id"));
        if (PyErr_Occurred()) {
            PyErr_Clear();
            error = "WebGPU particle graph batch identity is invalid";
            break;
        }
        error = g_particleRuntime->ReplaceGraph(static_cast<uint64_t>(graphId), PyDict_GetItemString(graph, "programs"),
                                                PyDict_GetItemString(graph, "remove_ids"));
    }
    Py_DECREF(sequence);
    return PyUnicode_FromString(error.c_str());
}

PyObject *UpdateGpuParticleParameters(PyObject *, PyObject *arguments)
{
    unsigned long long graphId = 0;
    PyObject *words = nullptr;
    if (!PyArg_ParseTuple(arguments, "KO:_update_gpu_particle_parameters", &graphId, &words))
        return nullptr;
    const std::string error = g_particleRuntime
                                  ? g_particleRuntime->UpdateParameters(static_cast<uint64_t>(graphId), words)
                                  : "WebGPU particle runtime is not initialized";
    return PyUnicode_FromString(error.c_str());
}

PyObject *BeginGpuParticleBatch(PyObject *, PyObject *arguments)
{
    unsigned long long graphId = 0;
    PyObject *items = nullptr;
    if (!PyArg_ParseTuple(arguments, "KO:_begin_gpu_particle_batch", &graphId, &items))
        return nullptr;
    return PyBool_FromLong(g_particleRuntime && g_particleRuntime->BeginBatch(static_cast<uint64_t>(graphId), items));
}

PyObject *SetGpuParticleEmitterPlaying(PyObject *, PyObject *arguments)
{
    unsigned long long emitterId = 0;
    int playing = 0;
    if (!PyArg_ParseTuple(arguments, "Kp:_set_gpu_particle_emitter_playing", &emitterId, &playing))
        return nullptr;
    return PyBool_FromLong(g_particleRuntime &&
                           g_particleRuntime->SetPlaying(static_cast<uint64_t>(emitterId), playing != 0));
}

PyObject *ResetGpuParticleEmitter(PyObject *, PyObject *arguments)
{
    unsigned long long emitterId = 0;
    if (!PyArg_ParseTuple(arguments, "K:_reset_gpu_particle_emitter", &emitterId))
        return nullptr;
    return PyBool_FromLong(g_particleRuntime && g_particleRuntime->Reset(static_cast<uint64_t>(emitterId)));
}

PyObject *GpuParticleArtifactRevision(PyObject *, PyObject *arguments)
{
    unsigned long long emitterId = 0;
    if (!PyArg_ParseTuple(arguments, "K:_gpu_particle_artifact_revision", &emitterId))
        return nullptr;
    return PyLong_FromUnsignedLongLong(
        g_particleRuntime ? g_particleRuntime->ArtifactRevision(static_cast<uint64_t>(emitterId)) : 0);
}

PyObject *BeginTextInput(PyObject *, PyObject *arguments)
{
    const char *initialValue = nullptr;
    const char *inputType = nullptr;
    if (!PyArg_ParseTuple(arguments, "ss:begin_text_input", &initialValue, &inputType))
        return nullptr;
    const bool started = infernux::InputManager::Instance().StartTextInput();
    if (!started)
        Py_RETURN_FALSE;
    EM_ASM(
        {
            if (Module.infernuxBeginTextInput)
                Module.infernuxBeginTextInput(UTF8ToString($0), UTF8ToString($1));
        },
        initialValue, inputType);
    g_textInputActive = true;
    Py_RETURN_TRUE;
}

PyObject *EndTextInput(PyObject *, PyObject *)
{
    InfernuxWebEndTextInput();
    Py_RETURN_NONE;
}

PyObject *IsTextInputActive(PyObject *, PyObject *)
{
    return PyBool_FromLong(g_textInputActive ? 1 : 0);
}

PyObject *ScreenUIBeginFrame(PyObject *, PyObject *arguments)
{
    unsigned int width = 0;
    unsigned int height = 0;
    if (!PyArg_ParseTuple(arguments, "II:screen_ui_begin_frame", &width, &height))
        return nullptr;
    if (g_screenUIRenderer)
        g_screenUIRenderer->BeginFrame(width, height);
    Py_RETURN_NONE;
}

PyObject *ScreenUIBeginFrameCached(PyObject *, PyObject *arguments)
{
    unsigned int width = 0;
    unsigned int height = 0;
    unsigned long long revision = 0;
    if (!PyArg_ParseTuple(arguments, "IIK:screen_ui_begin_frame_cached", &width, &height, &revision))
        return nullptr;
    return PyBool_FromLong(g_screenUIRenderer && g_screenUIRenderer->BeginFrameCached(width, height, revision));
}

PyObject *ScreenUIAddFilledRect(PyObject *, PyObject *arguments)
{
    int list = 0;
    double values[9]{};
    if (!PyArg_ParseTuple(arguments, "iddddddddd:screen_ui_add_filled_rect", &list, &values[0], &values[1], &values[2],
                          &values[3], &values[4], &values[5], &values[6], &values[7], &values[8]))
        return nullptr;
    if (g_screenUIRenderer)
        g_screenUIRenderer->AddFilledRect(
            list, static_cast<float>(values[0]), static_cast<float>(values[1]), static_cast<float>(values[2]),
            static_cast<float>(values[3]), static_cast<float>(values[4]), static_cast<float>(values[5]),
            static_cast<float>(values[6]), static_cast<float>(values[7]), static_cast<float>(values[8]));
    Py_RETURN_NONE;
}

double TupleNumber(PyObject *arguments, Py_ssize_t index)
{
    return PyFloat_AsDouble(PyTuple_GetItem(arguments, index));
}

PyObject *ScreenUIAddImage(PyObject *, PyObject *arguments)
{
    if (PyTuple_Size(arguments) != 18) {
        PyErr_SetString(PyExc_TypeError, "screen_ui_add_image expects 18 arguments");
        return nullptr;
    }
    const int list = static_cast<int>(PyLong_AsLong(PyTuple_GetItem(arguments, 0)));
    const uint64_t texture = PyLong_AsUnsignedLongLong(PyTuple_GetItem(arguments, 1));
    double values[13]{};
    for (int index = 0; index < 13; ++index)
        values[index] = TupleNumber(arguments, index + 2);
    const bool mirrorH = PyObject_IsTrue(PyTuple_GetItem(arguments, 15)) != 0;
    const bool mirrorV = PyObject_IsTrue(PyTuple_GetItem(arguments, 16)) != 0;
    const double rounding = TupleNumber(arguments, 17);
    if (PyErr_Occurred())
        return nullptr;
    if (g_screenUIRenderer)
        g_screenUIRenderer->AddImage(
            list, texture, static_cast<float>(values[0]), static_cast<float>(values[1]), static_cast<float>(values[2]),
            static_cast<float>(values[3]), static_cast<float>(values[4]), static_cast<float>(values[5]),
            static_cast<float>(values[6]), static_cast<float>(values[7]), static_cast<float>(values[8]),
            static_cast<float>(values[9]), static_cast<float>(values[10]), static_cast<float>(values[11]),
            static_cast<float>(values[12]), mirrorH, mirrorV, static_cast<float>(rounding));
    Py_RETURN_NONE;
}

PyObject *ScreenUIAddText(PyObject *, PyObject *arguments)
{
    if (PyTuple_Size(arguments) != 20) {
        PyErr_SetString(PyExc_TypeError, "screen_ui_add_text expects 20 arguments");
        return nullptr;
    }
    const int list = static_cast<int>(PyLong_AsLong(PyTuple_GetItem(arguments, 0)));
    const char *text = PyUnicode_AsUTF8(PyTuple_GetItem(arguments, 5));
    const bool mirrorH = PyObject_IsTrue(PyTuple_GetItem(arguments, 15)) != 0;
    const bool mirrorV = PyObject_IsTrue(PyTuple_GetItem(arguments, 16)) != 0;
    const char *fontPath = PyUnicode_AsUTF8(PyTuple_GetItem(arguments, 17));
    double values[15]{};
    for (int index = 0; index < 4; ++index)
        values[index] = TupleNumber(arguments, index + 1);
    for (int index = 4; index < 13; ++index)
        values[index] = TupleNumber(arguments, index + 2);
    values[13] = TupleNumber(arguments, 18);
    values[14] = TupleNumber(arguments, 19);
    if (PyErr_Occurred() || !text || !fontPath)
        return nullptr;
    if (g_screenUIRenderer)
        g_screenUIRenderer->AddText(
            list, static_cast<float>(values[0]), static_cast<float>(values[1]), static_cast<float>(values[2]),
            static_cast<float>(values[3]), text, static_cast<float>(values[4]), static_cast<float>(values[5]),
            static_cast<float>(values[6]), static_cast<float>(values[7]), static_cast<float>(values[8]),
            static_cast<float>(values[9]), static_cast<float>(values[10]), static_cast<float>(values[11]),
            static_cast<float>(values[12]), mirrorH, mirrorV, fontPath, static_cast<float>(values[13]),
            static_cast<float>(values[14]));
    Py_RETURN_NONE;
}

PyObject *ScreenUIMeasureText(PyObject *, PyObject *arguments)
{
    const char *text = nullptr;
    const char *fontPath = nullptr;
    double fontSize = 0.0;
    double wrapWidth = 0.0;
    double lineHeight = 0.0;
    double letterSpacing = 0.0;
    if (!PyArg_ParseTuple(arguments, "sddsdd:screen_ui_measure_text", &text, &fontSize, &wrapWidth, &fontPath,
                          &lineHeight, &letterSpacing))
        return nullptr;
    const auto measured = g_screenUIRenderer ? g_screenUIRenderer->MeasureText(text, static_cast<float>(fontSize),
                                                                               static_cast<float>(wrapWidth), fontPath,
                                                                               static_cast<float>(lineHeight),
                                                                               static_cast<float>(letterSpacing))
                                             : std::pair<float, float>{0.0f, 0.0f};
    return Py_BuildValue("ff", measured.first, measured.second);
}

PyMethodDef kMethods[] = {
    {"read_entry", ReadPackageEntry, METH_VARARGS,
     "Read and validate one entry from the native Infernux Player container."},
    {"extract_package", ExtractPackage, METH_VARARGS, "Validate and extract one native Infernux Player container."},
    {"register_shader", RegisterShader, METH_VARARGS,
     "Register one validated WGSL shader in the browser runtime catalog."},
    {"initialize_runtime_assets", InitializeRuntimeAssets, METH_VARARGS,
     "Install the immutable cooked GUID catalog and runtime asset loaders."},
    {"_replace_gpu_particle_graph", ReplaceGpuParticleGraph, METH_VARARGS, nullptr},
    {"_replace_gpu_particle_graphs", ReplaceGpuParticleGraphs, METH_VARARGS, nullptr},
    {"_update_gpu_particle_parameters", UpdateGpuParticleParameters, METH_VARARGS, nullptr},
    {"_begin_gpu_particle_batch", BeginGpuParticleBatch, METH_VARARGS, nullptr},
    {"_set_gpu_particle_emitter_playing", SetGpuParticleEmitterPlaying, METH_VARARGS, nullptr},
    {"_reset_gpu_particle_emitter", ResetGpuParticleEmitter, METH_VARARGS, nullptr},
    {"_gpu_particle_artifact_revision", GpuParticleArtifactRevision, METH_VARARGS, nullptr},
    {"begin_text_input", BeginTextInput, METH_VARARGS, "Focus the browser text bridge and begin committed text input."},
    {"end_text_input", EndTextInput, METH_NOARGS, "End browser text input and dismiss the software keyboard."},
    {"is_text_input_active", IsTextInputActive, METH_NOARGS, "Return whether browser text input is active."},
    {"screen_ui_begin_frame", ScreenUIBeginFrame, METH_VARARGS, nullptr},
    {"screen_ui_begin_frame_cached", ScreenUIBeginFrameCached, METH_VARARGS, nullptr},
    {"screen_ui_add_filled_rect", ScreenUIAddFilledRect, METH_VARARGS, nullptr},
    {"screen_ui_add_image", ScreenUIAddImage, METH_VARARGS, nullptr},
    {"screen_ui_add_text", ScreenUIAddText, METH_VARARGS, nullptr},
    {"screen_ui_measure_text", ScreenUIMeasureText, METH_VARARGS, nullptr},
    {nullptr, nullptr, 0, nullptr},
};

PyModuleDef kModule = {
    PyModuleDef_HEAD_INIT,
    "_InfernuxWebHost",
    "Web Player host services that do not belong to the gameplay API.",
    -1,
    kMethods,
};

} // namespace

PyMODINIT_FUNC PyInit__InfernuxWebHost()
{
    return PyModule_Create(&kModule);
}

bool InfernuxWebFindShaderSource(const std::string &name, const char *stage, std::string &source)
{
    std::lock_guard lock(g_shaderMutex);
    const auto found = g_shaderSources.find(ShaderKey(name, stage));
    if (found == g_shaderSources.end())
        return false;
    source = found->second;
    return true;
}

void InfernuxWebSetParticleRuntime(infernux::web::WebParticleRuntime *runtime) noexcept
{
    g_particleRuntime = runtime;
}

void InfernuxWebSetScreenUIRenderer(infernux::web::WebScreenUIRenderer *renderer) noexcept
{
    g_screenUIRenderer = renderer;
}

void InfernuxWebEndTextInput() noexcept
{
    EM_ASM({
        if (Module.infernuxEndTextInput)
            Module.infernuxEndTextInput();
    });
    infernux::InputManager::Instance().StopTextInput();
    g_textInputActive = false;
}
