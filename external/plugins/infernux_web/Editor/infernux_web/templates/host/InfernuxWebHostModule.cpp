#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include "InfernuxWebHostModule.h"

#include <platform/filesystem/InxPack.h>

#if defined(INFERNUX_WEB_ENGINE_RUNTIME)
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

PyMethodDef kMethods[] = {
    {"read_entry", ReadPackageEntry, METH_VARARGS,
     "Read and validate one entry from the native Infernux Player container."},
    {"extract_package", ExtractPackage, METH_VARARGS, "Validate and extract one native Infernux Player container."},
    {"register_shader", RegisterShader, METH_VARARGS,
     "Register one validated WGSL shader in the browser runtime catalog."},
    {"initialize_runtime_assets", InitializeRuntimeAssets, METH_VARARGS,
     "Install the immutable cooked GUID catalog and runtime asset loaders."},
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
