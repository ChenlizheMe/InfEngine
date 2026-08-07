#include "InxFileLoader/InxTextureLoader.hpp"
#include "InxResource/InxResourceMeta.h"
#include "JsonPyBridge.h"
#include <function/resources/AssetDatabase/AssetDatabase.h>
#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/resources/PhysicMaterial/PhysicMaterial.h>
#include <platform/filesystem/DocumentStore.h>

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <stdexcept>

namespace py = pybind11;

namespace infernux
{

namespace
{

py::object ShaderReferenceToPython(const ShaderAssetReference &reference)
{
    return JsonToPython({
        {"guid", reference.guid},
        {"shader_id", reference.shaderId},
        {"path_hint", reference.pathHint},
    });
}

ShaderAssetReference ShaderReferenceFromPython(py::handle value)
{
    const nlohmann::json document = PythonToJson(value);
    if (!document.is_object())
        throw std::invalid_argument("shader reference must be a dictionary");
    for (const char *field : {"guid", "shader_id", "path_hint"}) {
        if (!document.contains(field) || !document[field].is_string())
            throw std::invalid_argument(std::string("shader reference field '") + field + "' must be a string");
    }
    ShaderAssetReference reference{
        document["guid"].get<std::string>(),
        document["shader_id"].get<std::string>(),
        document["path_hint"].get<std::string>(),
    };
    if (!reference.IsAssigned())
        throw std::invalid_argument("shader reference requires guid or shader_id");
    return reference;
}

} // namespace

void RegisterResourceBindings(py::module_ &m)
{
    py::register_exception<DocumentWriteSuperseded>(m, "DocumentWriteSuperseded");
    py::register_exception<DocumentWriteCancelled>(m, "DocumentWriteCancelled");

    py::class_<DocumentFileState>(m, "DocumentFileState")
        .def(py::init<>())
        .def_readwrite("exists", &DocumentFileState::exists)
        .def_readwrite("size", &DocumentFileState::size)
        .def_readwrite("modified_ns", &DocumentFileState::modifiedNs)
        .def_readwrite("content_hash", &DocumentFileState::contentHash);

    py::class_<DocumentWriteOptions>(m, "DocumentWriteOptions")
        .def(py::init<>())
        .def_readwrite("create_backup", &DocumentWriteOptions::createBackup)
        .def_readwrite("expected_file_state", &DocumentWriteOptions::expectedFileState);

    py::class_<DocumentPathMetrics>(m, "DocumentPathMetrics")
        .def_readonly("latest_submitted_generation", &DocumentPathMetrics::latestSubmittedGeneration)
        .def_readonly("latest_succeeded_generation", &DocumentPathMetrics::latestSucceededGeneration)
        .def_readonly("latest_failed_generation", &DocumentPathMetrics::latestFailedGeneration)
        .def_readonly("pending_generation", &DocumentPathMetrics::pendingGeneration)
        .def_readonly("active_generation", &DocumentPathMetrics::activeGeneration);

    py::class_<DocumentWriteTicket, std::shared_ptr<DocumentWriteTicket>>(m, "DocumentWriteTicket")
        .def_property_readonly("path", &DocumentWriteTicket::GetPath)
        .def_property_readonly("generation", &DocumentWriteTicket::GetGeneration)
        .def_property_readonly("is_complete", &DocumentWriteTicket::IsComplete)
        .def_property_readonly("status", &DocumentWriteTicket::GetStatusName)
        .def_property_readonly("error", &DocumentWriteTicket::GetError)
        .def_property_readonly("committed_file_state", &DocumentWriteTicket::GetCommittedFileState)
        .def("wait", &DocumentWriteTicket::Wait, py::call_guard<py::gil_scoped_release>());

    py::class_<DocumentStore>(m, "NativeDocumentStore")
        .def_static("instance", &DocumentStore::Instance, py::return_value_policy::reference)
        .def("submit", &DocumentStore::Submit, py::arg("path"), py::arg("content"),
             py::arg("options") = DocumentWriteOptions{})
        .def("write_and_wait", &DocumentStore::WriteAndWait, py::arg("path"), py::arg("content"),
             py::arg("options") = DocumentWriteOptions{}, py::call_guard<py::gil_scoped_release>())
        .def("cancel", &DocumentStore::Cancel, py::arg("ticket"))
        .def("get_metrics", &DocumentStore::GetMetrics, py::arg("path"))
        .def("capture_file_state", &DocumentStore::CaptureFileState, py::arg("path"))
        .def("flush_all", py::overload_cast<>(&DocumentStore::Flush), py::call_guard<py::gil_scoped_release>())
        .def("flush_path", py::overload_cast<const std::string &>(&DocumentStore::Flush), py::arg("path"),
             py::call_guard<py::gil_scoped_release>())
        .def("shutdown", &DocumentStore::Shutdown, py::call_guard<py::gil_scoped_release>());

    // ResourceType enum
    py::enum_<ResourceType>(m, "ResourceType")
        .value("Meta", ResourceType::Meta)
        .value("Shader", ResourceType::Shader)
        .value("Texture", ResourceType::Texture)
        .value("Mesh", ResourceType::Mesh)
        .value("Material", ResourceType::Material)
        .value("Script", ResourceType::Script)
        .value("Audio", ResourceType::Audio)
        .value("DefaultText", ResourceType::DefaultText)
        .value("DefaultBinary", ResourceType::DefaultBinary)
        .value("PhysicMaterial", ResourceType::PhysicMaterial)
        .value("RenderEffect", ResourceType::RenderEffect)
        .value("ParticleGraph", ResourceType::ParticleGraph)
        .export_values();

    // InxResourceMeta - resource metadata
    py::class_<InxResourceMeta, std::shared_ptr<InxResourceMeta>>(m, "ResourceMeta")
        .def(py::init<>())
        .def("get_resource_name", &InxResourceMeta::GetResourceName,
             "Get the resource name (filename without extension)")
        .def("get_guid", &InxResourceMeta::GetGuid, "Get the stable GUID for this resource")
        .def("get_resource_type", &InxResourceMeta::GetResourceType, "Get the resource type")
        .def("has_key", &InxResourceMeta::HasKey, py::arg("key"), "Check if metadata has a specific key")
        .def(
            "get_string",
            [](const InxResourceMeta &self, const std::string &key) {
                if (!self.HasKey(key))
                    return std::string("");
                return self.GetDataAs<std::string>(key);
            },
            py::arg("key"), "Get a string metadata value")
        .def(
            "get_int",
            [](const InxResourceMeta &self, const std::string &key) {
                if (!self.HasKey(key))
                    return 0;
                return self.GetDataAs<int>(key);
            },
            py::arg("key"), "Get an integer metadata value")
        .def(
            "get_float",
            [](const InxResourceMeta &self, const std::string &key) {
                if (!self.HasKey(key))
                    return 0.0f;
                return self.GetDataAs<float>(key);
            },
            py::arg("key"), "Get a float metadata value")
        .def("serialize_document", [](const InxResourceMeta &self) { return JsonToPython(self.SerializeDocument()); })
        .def(
            "deserialize_document",
            [](InxResourceMeta &self, const py::object &document) { self.DeserializeDocument(PythonToJson(document)); },
            py::arg("document"));

    py::class_<PhysicMaterial, std::shared_ptr<PhysicMaterial>>(m, "InxPhysicMaterial")
        .def(py::init<>())
        .def_property_readonly("name", &PhysicMaterial::GetName)
        .def_property_readonly("guid", &PhysicMaterial::GetGuid)
        .def_property_readonly("file_path", &PhysicMaterial::GetFilePath)
        .def_property("friction", &PhysicMaterial::GetFriction, &PhysicMaterial::SetFriction)
        .def_property("bounciness", &PhysicMaterial::GetBounciness, &PhysicMaterial::SetBounciness)
        .def_property(
            "friction_combine",
            [](const PhysicMaterial &material) { return static_cast<int>(material.GetFrictionCombine()); },
            [](PhysicMaterial &material, int value) {
                material.SetFrictionCombine(static_cast<PhysicsMaterialCombine>(value));
            })
        .def_property(
            "bounce_combine",
            [](const PhysicMaterial &material) { return static_cast<int>(material.GetBounceCombine()); },
            [](PhysicMaterial &material, int value) {
                material.SetBounceCombine(static_cast<PhysicsMaterialCombine>(value));
            })
        .def("serialize_document",
             [](const PhysicMaterial &material) { return JsonToPython(material.SerializeDocument()); })
        .def("deserialize_document", [](PhysicMaterial &material,
                                        py::handle document) { material.DeserializeDocument(PythonToJson(document)); })
        .def("save", py::overload_cast<>(&PhysicMaterial::SaveToFile, py::const_))
        .def("save_to", py::overload_cast<const std::string &>(&PhysicMaterial::SaveToFile), py::arg("path"));

    // InxTextureData - raw texture data accessible from Python
    py::class_<InxTextureData>(m, "TextureData")
        .def(py::init<>())
        .def_readonly("width", &InxTextureData::width, "Texture width in pixels")
        .def_readonly("height", &InxTextureData::height, "Texture height in pixels")
        .def_readonly("channels", &InxTextureData::channels, "Number of color channels (always 4 for RGBA)")
        .def_readonly("name", &InxTextureData::name, "Texture name/identifier")
        .def_readonly("source_path", &InxTextureData::sourcePath, "Original file path")
        .def("is_valid", &InxTextureData::IsValid, "Check if texture data is valid")
        .def("get_size_bytes", &InxTextureData::GetSizeBytes, "Get total size in bytes")
        .def(
            "get_pixels",
            [](const InxTextureData &self) {
                // Return pixels as bytes for Python access
                return py::bytes(reinterpret_cast<const char *>(self.pixels.data()), self.pixels.size());
            },
            "Get raw pixel data as bytes (RGBA format)")
        .def(
            "get_pixels_list",
            [](const InxTextureData &self) {
                // Return pixels as RGBA8 bytes for submit_imgui_texture.
                return self.pixels;
            },
            "Get raw RGBA8 pixel data for submit_imgui_texture");

    // InxTextureLoader - static methods for loading textures
    py::class_<InxTextureLoader>(m, "TextureLoader")
        .def_static("load_from_file", &InxTextureLoader::LoadFromFile, py::arg("file_path"), py::arg("name") = "",
                    "Load texture from file")
        .def_static(
            "load_from_memory",
            [](py::bytes data, const std::string &name) {
                std::string str = data;
                return InxTextureLoader::LoadFromMemory(reinterpret_cast<const unsigned char *>(str.data()), str.size(),
                                                        name);
            },
            py::arg("data"), py::arg("name") = "", "Load texture from memory buffer")
        .def_static("create_solid_color", &InxTextureLoader::CreateSolidColor, py::arg("width"), py::arg("height"),
                    py::arg("r"), py::arg("g"), py::arg("b"), py::arg("a"), py::arg("name") = "solid_color",
                    "Create a solid color texture")
        .def_static("create_checkerboard", &InxTextureLoader::CreateCheckerboard, py::arg("width"), py::arg("height"),
                    py::arg("checker_size") = 8, py::arg("name") = "checkerboard",
                    "Create a checkerboard texture (for error indication)");

    // InxMaterial - material definition (named InxMaterial to avoid conflict with ResourceType.Material)
    py::class_<InxMaterial, std::shared_ptr<InxMaterial>>(m, "InxMaterial")
        .def(py::init<>())
        .def(py::init<const std::string &>(), py::arg("name"))
        .def(py::init<const std::string &, const std::string &>(), py::arg("name"), py::arg("shader_name"))
        .def_property("name", &InxMaterial::GetName, &InxMaterial::SetName, "Material name")
        .def_property_readonly("guid", &InxMaterial::GetGuid, "Material GUID (read-only, set by AssetDatabase)")
        .def_property("file_path", &InxMaterial::GetFilePath, &InxMaterial::SetFilePath, "File path for saving")
        .def_property_readonly("material_key", &InxMaterial::GetMaterialKey,
                               "Unique key used by the pipeline manager (GUID > filePath > name)")
        .def_property("is_builtin", &InxMaterial::IsBuiltin, &InxMaterial::SetBuiltin,
                      "Whether this is a built-in material (shader cannot be changed)")
        .def_property(
            "shader_name", &InxMaterial::GetShaderName,
            [](InxMaterial &m, const std::string &name) { m.SetShader(name); },
            "Shader name (e.g. 'lit', 'unlit') — sets both vert and frag")
        .def_property(
            "vert_shader_name", &InxMaterial::GetVertShaderName,
            [](InxMaterial &m, const std::string &name) { m.SetVertShader(name); }, "Vertex shader name")
        .def_property(
            "frag_shader_name", &InxMaterial::GetFragShaderName,
            [](InxMaterial &m, const std::string &name) { m.SetFragShader(name); }, "Fragment shader name")
        .def_property(
            "vert_shader_reference",
            [](const InxMaterial &material) { return ShaderReferenceToPython(material.GetVertShaderReference()); },
            [](InxMaterial &material, py::handle reference) {
                material.SetVertShaderReference(ShaderReferenceFromPython(reference));
            },
            "Stable vertex shader asset reference")
        .def_property(
            "frag_shader_reference",
            [](const InxMaterial &material) { return ShaderReferenceToPython(material.GetFragShaderReference()); },
            [](InxMaterial &material, py::handle reference) {
                material.SetFragShaderReference(ShaderReferenceFromPython(reference));
            },
            "Stable fragment shader asset reference")
        .def("set_shader", &InxMaterial::SetShader, py::arg("shader_name"),
             "Set the material's shader by name (sets both vert and frag)")
        .def("get_render_queue", &InxMaterial::GetRenderQueue, "Get the render queue value")
        .def("set_render_queue", &InxMaterial::SetRenderQueue, py::arg("queue"), "Set the render queue value")
        .def("save", py::overload_cast<>(&InxMaterial::SaveToFile, py::const_), "Save material to its file path")
        .def("save_to", py::overload_cast<const std::string &>(&InxMaterial::SaveToFile), py::arg("path"),
             "Save material to specified path")
        .def("is_deleted", &InxMaterial::IsDeleted, "True if the backing .mat file has been deleted")
        .def("mark_as_deleted", &InxMaterial::MarkAsDeleted, "Mark this material as deleted (prevents save)")
        // Property setters (accept both tuple and individual args)
        .def("set_float", &InxMaterial::SetFloat, py::arg("name"), py::arg("value"), "Set a float property")
        .def(
            "set_vector2",
            [](InxMaterial &mat, const std::string &name, py::args args) {
                glm::vec2 v;
                if (args.size() == 1) {
                    py::object obj = args[0];
                    if (py::isinstance<py::tuple>(obj) || py::isinstance<py::list>(obj)) {
                        py::sequence seq = obj.cast<py::sequence>();
                        v = glm::vec2(seq[0].cast<float>(), seq[1].cast<float>());
                    } else {
                        v = obj.cast<glm::vec2>();
                    }
                } else if (args.size() >= 2) {
                    v = glm::vec2(args[0].cast<float>(), args[1].cast<float>());
                } else {
                    throw std::runtime_error("set_vector2: expected (name, x, y) or (name, vec2)");
                }
                mat.SetVector2(name, v);
            },
            py::arg("name"), "Set a vec2 property: set_vector2(name, x, y) or set_vector2(name, (x,y))")
        .def(
            "set_vector3",
            [](InxMaterial &mat, const std::string &name, py::args args) {
                glm::vec3 v;
                if (args.size() == 1) {
                    py::object obj = args[0];
                    if (py::isinstance<py::tuple>(obj) || py::isinstance<py::list>(obj)) {
                        py::sequence seq = obj.cast<py::sequence>();
                        v = glm::vec3(seq[0].cast<float>(), seq[1].cast<float>(), seq[2].cast<float>());
                    } else {
                        v = obj.cast<glm::vec3>();
                    }
                } else if (args.size() >= 3) {
                    v = glm::vec3(args[0].cast<float>(), args[1].cast<float>(), args[2].cast<float>());
                } else {
                    throw std::runtime_error("set_vector3: expected (name, x, y, z) or (name, vec3)");
                }
                mat.SetVector3(name, v);
            },
            py::arg("name"), "Set a vec3 property: set_vector3(name, x, y, z) or set_vector3(name, (x,y,z))")
        .def(
            "set_vector4",
            [](InxMaterial &mat, const std::string &name, py::args args) {
                glm::vec4 v;
                if (args.size() == 1) {
                    py::object obj = args[0];
                    if (py::isinstance<py::tuple>(obj) || py::isinstance<py::list>(obj)) {
                        py::sequence seq = obj.cast<py::sequence>();
                        v = glm::vec4(seq[0].cast<float>(), seq[1].cast<float>(), seq[2].cast<float>(),
                                      seq[3].cast<float>());
                    } else {
                        v = obj.cast<glm::vec4>();
                    }
                } else if (args.size() >= 4) {
                    v = glm::vec4(args[0].cast<float>(), args[1].cast<float>(), args[2].cast<float>(),
                                  args[3].cast<float>());
                } else {
                    throw std::runtime_error("set_vector4: expected (name, x, y, z, w) or (name, vec4)");
                }
                mat.SetVector4(name, v);
            },
            py::arg("name"), "Set a vec4 property: set_vector4(name, x, y, z, w) or set_vector4(name, (x,y,z,w))")
        .def(
            "set_color",
            [](InxMaterial &mat, const std::string &name, py::args args) {
                glm::vec4 color;
                if (args.size() == 1) {
                    py::object obj = args[0];
                    if (py::isinstance<py::tuple>(obj) || py::isinstance<py::list>(obj)) {
                        py::sequence seq = obj.cast<py::sequence>();
                        color = glm::vec4(seq[0].cast<float>(), seq[1].cast<float>(), seq[2].cast<float>(),
                                          py::len(seq) >= 4 ? seq[3].cast<float>() : 1.0f);
                    } else {
                        color = obj.cast<glm::vec4>();
                    }
                } else if (args.size() >= 3) {
                    float r = args[0].cast<float>();
                    float g = args[1].cast<float>();
                    float b = args[2].cast<float>();
                    float a = args.size() >= 4 ? args[3].cast<float>() : 1.0f;
                    color = glm::vec4(r, g, b, a);
                } else {
                    throw std::runtime_error("set_color: expected (name, r, g, b[, a]) or (name, color_tuple)");
                }
                mat.SetColor(name, color);
            },
            py::arg("name"), "Set a color property: set_color(name, r, g, b[, a]) or set_color(name, (r,g,b,a))")
        .def("set_int", &InxMaterial::SetInt, py::arg("name"), py::arg("value"), "Set an int property")
        .def("set_matrix", &InxMaterial::SetMatrix, py::arg("name"), py::arg("value"), "Set a mat4 property")
        .def("set_texture_guid", &InxMaterial::SetTextureGuid, py::arg("name"), py::arg("texture_guid"),
             "Set a texture property by GUID")
        .def(
            "set_param",
            [](InxMaterial &mat, const std::string &name, py::object value) {
                const MaterialProperty *prop = mat.GetProperty(name);

                if (py::isinstance<py::bool_>(value)) {
                    mat.SetInt(name, value.cast<bool>() ? 1 : 0);
                    return;
                }
                if (py::isinstance<py::int_>(value) && !py::isinstance<py::bool_>(value)) {
                    if (prop && prop->type == MaterialPropertyType::Float) {
                        mat.SetFloat(name, value.cast<float>());
                    } else {
                        mat.SetInt(name, value.cast<int>());
                    }
                    return;
                }
                if (py::isinstance<py::float_>(value)) {
                    if (prop && prop->type == MaterialPropertyType::Int) {
                        mat.SetInt(name, static_cast<int>(value.cast<float>()));
                    } else {
                        mat.SetFloat(name, value.cast<float>());
                    }
                    return;
                }
                if (py::isinstance<py::tuple>(value) || py::isinstance<py::list>(value)) {
                    py::sequence seq = value.cast<py::sequence>();
                    const auto len = py::len(seq);
                    if (len == 2) {
                        mat.SetVector2(name, glm::vec2(seq[0].cast<float>(), seq[1].cast<float>()));
                        return;
                    }
                    if (len == 3) {
                        mat.SetVector3(name,
                                       glm::vec3(seq[0].cast<float>(), seq[1].cast<float>(), seq[2].cast<float>()));
                        return;
                    }
                    if (len == 4) {
                        glm::vec4 v(seq[0].cast<float>(), seq[1].cast<float>(), seq[2].cast<float>(),
                                    seq[3].cast<float>());
                        if (prop && prop->type == MaterialPropertyType::Color) {
                            mat.SetColor(name, v);
                        } else {
                            mat.SetVector4(name, v);
                        }
                        return;
                    }
                    if (len == 16) {
                        glm::mat4 m(1.0f);
                        for (int i = 0; i < 16; ++i) {
                            m[i / 4][i % 4] = seq[i].cast<float>();
                        }
                        mat.SetMatrix(name, m);
                        return;
                    }
                }

                throw std::runtime_error(
                    "set_param: unsupported value type. Expected int/float/bool, vec2/3/4 tuple, or 16-float matrix.");
            },
            py::arg("name"), py::arg("value"), "Set a non-texture material property using value-shape/type dispatch")
        .def("clear_texture", &InxMaterial::ClearTexture, py::arg("name"),
             "Clear a texture property (remove texture reference)")
        .def("remove_property", &InxMaterial::RemoveProperty, py::arg("name"),
             "Remove a material property and its asset dependency")
        .def(
            "set_texture",
            [](InxMaterial &mat, const std::string &name, py::object value) {
                if (value.is_none()) {
                    mat.ClearTexture(name);
                    return;
                }

                auto applyString = [&](const std::string &text) {
                    if (text.empty()) {
                        mat.ClearTexture(name);
                        return;
                    }
                    mat.SetTextureGuid(name, text);
                };

                if (py::isinstance<py::str>(value)) {
                    applyString(value.cast<std::string>());
                    return;
                }

                if (py::hasattr(value, "guid")) {
                    py::object guidObj = value.attr("guid");
                    if (!guidObj.is_none()) {
                        std::string guid = py::cast<std::string>(guidObj);
                        if (!guid.empty()) {
                            mat.SetTextureGuid(name, guid);
                            return;
                        }
                    }
                }

                throw std::runtime_error(
                    "set_texture: expected None, a Texture GUID/builtin token, or an object with guid.");
            },
            py::arg("name"), py::arg("value"), "Set a texture property from a Texture GUID or texture object")
        // Individual property getters (convenience wrappers over GetProperty)
        .def(
            "get_float",
            [](const InxMaterial &mat, const std::string &name, float defaultVal) -> float {
                const MaterialProperty *prop = mat.GetProperty(name);
                if (prop && prop->type == MaterialPropertyType::Float)
                    return std::get<float>(prop->value);
                return defaultVal;
            },
            py::arg("name"), py::arg("default_value") = 0.0f, "Get a float property")
        .def(
            "get_int",
            [](const InxMaterial &mat, const std::string &name, int defaultVal) -> int {
                const MaterialProperty *prop = mat.GetProperty(name);
                if (prop && prop->type == MaterialPropertyType::Int)
                    return std::get<int>(prop->value);
                return defaultVal;
            },
            py::arg("name"), py::arg("default_value") = 0, "Get an int property")
        .def(
            "get_color",
            [](const InxMaterial &mat, const std::string &name) -> glm::vec4 {
                const MaterialProperty *prop = mat.GetProperty(name);
                if (prop && (prop->type == MaterialPropertyType::Float4 || prop->type == MaterialPropertyType::Color)) {
                    return std::get<glm::vec4>(prop->value);
                }
                return glm::vec4(0.0f, 0.0f, 0.0f, 1.0f);
            },
            py::arg("name"), "Get a color property as vec4f")
        .def(
            "get_vector2",
            [](const InxMaterial &mat, const std::string &name) -> glm::vec2 {
                const MaterialProperty *prop = mat.GetProperty(name);
                if (prop && prop->type == MaterialPropertyType::Float2) {
                    return std::get<glm::vec2>(prop->value);
                }
                return glm::vec2(0.0f);
            },
            py::arg("name"), "Get a vec2 property as Vector2")
        .def(
            "get_vector3",
            [](const InxMaterial &mat, const std::string &name) -> glm::vec3 {
                const MaterialProperty *prop = mat.GetProperty(name);
                if (prop && prop->type == MaterialPropertyType::Float3) {
                    return std::get<glm::vec3>(prop->value);
                }
                return glm::vec3(0.0f);
            },
            py::arg("name"), "Get a vec3 property as Vector3")
        .def(
            "get_vector4",
            [](const InxMaterial &mat, const std::string &name) -> glm::vec4 {
                const MaterialProperty *prop = mat.GetProperty(name);
                if (prop && (prop->type == MaterialPropertyType::Float4 || prop->type == MaterialPropertyType::Color)) {
                    return std::get<glm::vec4>(prop->value);
                }
                return glm::vec4(0.0f);
            },
            py::arg("name"), "Get a vec4 property as vec4f")
        .def(
            "get_texture",
            [](const InxMaterial &mat, const std::string &name) -> py::object {
                const MaterialProperty *prop = mat.GetProperty(name);
                if (prop && prop->type == MaterialPropertyType::Texture2D)
                    return py::cast(std::get<std::string>(prop->value));
                return py::none();
            },
            py::arg("name"), "Get a texture property GUID (or None)")
        // Generic property access
        .def("has_property", &InxMaterial::HasProperty, py::arg("name"), "Check if material has a property")
        .def(
            "get_property",
            [](const InxMaterial &mat, const std::string &name) -> py::object {
                const MaterialProperty *prop = mat.GetProperty(name);
                if (!prop) {
                    return py::none();
                }
                // Return the property value as appropriate Python type
                switch (prop->type) {
                case MaterialPropertyType::Float:
                    return py::cast(std::get<float>(prop->value));
                case MaterialPropertyType::Float2:
                    return py::cast(std::get<glm::vec2>(prop->value));
                case MaterialPropertyType::Float3:
                    return py::cast(std::get<glm::vec3>(prop->value));
                case MaterialPropertyType::Float4:
                case MaterialPropertyType::Color:
                    return py::cast(std::get<glm::vec4>(prop->value));
                case MaterialPropertyType::Int:
                    return py::cast(std::get<int>(prop->value));
                case MaterialPropertyType::Mat4: {
                    // mat4 not registered as pybind11 type — return as list of 16 floats
                    auto &m = std::get<glm::mat4>(prop->value);
                    py::list result;
                    const float *data = &m[0][0];
                    for (int i = 0; i < 16; ++i)
                        result.append(data[i]);
                    return result;
                }
                case MaterialPropertyType::Texture2D:
                    return py::cast(std::get<std::string>(prop->value));
                }
                return py::none();
            },
            py::arg("name"), "Get a property value by name")
        .def(
            "get_all_properties",
            [](const InxMaterial &mat) -> py::dict {
                py::dict result;
                for (const auto &[name, prop] : mat.GetAllProperties()) {
                    switch (prop.type) {
                    case MaterialPropertyType::Float:
                        result[py::str(name)] = std::get<float>(prop.value);
                        break;
                    case MaterialPropertyType::Float2:
                        result[py::str(name)] = py::cast(std::get<glm::vec2>(prop.value));
                        break;
                    case MaterialPropertyType::Float3:
                        result[py::str(name)] = py::cast(std::get<glm::vec3>(prop.value));
                        break;
                    case MaterialPropertyType::Float4:
                    case MaterialPropertyType::Color:
                        result[py::str(name)] = py::cast(std::get<glm::vec4>(prop.value));
                        break;
                    case MaterialPropertyType::Int:
                        result[py::str(name)] = std::get<int>(prop.value);
                        break;
                    case MaterialPropertyType::Mat4: {
                        // mat4 not registered as pybind11 type — return as list of 16 floats
                        auto &m4 = std::get<glm::mat4>(prop.value);
                        py::list ml;
                        const float *data = &m4[0][0];
                        for (int i = 0; i < 16; ++i)
                            ml.append(data[i]);
                        result[py::str(name)] = ml;
                        break;
                    }
                    case MaterialPropertyType::Texture2D:
                        result[py::str(name)] = std::get<std::string>(prop.value);
                        break;
                    }
                }
                return result;
            },
            "Get all properties as a dictionary")
        // Pipeline state
        .def("is_pipeline_dirty", &InxMaterial::IsPipelineDirty, "Check if pipeline needs recreation")
        .def("clear_pipeline_dirty", &InxMaterial::ClearPipelineDirty, "Clear the pipeline dirty flag")
        .def("get_pipeline_hash", &InxMaterial::GetPipelineHash, "Get a hash of the pipeline configuration")
        .def("get_version", &InxMaterial::GetVersion, "Monotonic version counter incremented on every property change")
        // Serialization
        .def("serialize", &InxMaterial::Serialize, "Serialize material to JSON string")
        .def("deserialize", &InxMaterial::Deserialize, py::arg("json_str"), "Deserialize material from JSON string")
        .def(
            "serialize_document",
            [](const InxMaterial &material) { return JsonToPython(material.SerializeDocument()); },
            "Serialize material to a Python document")
        .def(
            "deserialize_document",
            [](InxMaterial &material, py::handle document) {
                return material.DeserializeDocument(PythonToJson(document));
            },
            py::arg("document"), "Deserialize material from a Python document")
        .def_static("create_default_lit", &InxMaterial::CreateDefaultLit,
                    "Create the default lit opaque material (built-in)")
        .def_static("create_default_unlit", &InxMaterial::CreateDefaultUnlit, "Create a default unlit opaque material")
        // Render state access
        .def(
            "get_render_state", [](const InxMaterial &mat) -> RenderState { return mat.GetRenderState(); },
            "Get a copy of the material's render state")
        .def(
            "set_render_state", [](InxMaterial &mat, const RenderState &state) { mat.SetRenderState(state); },
            py::arg("state"), "Set the material's render state")
        // RenderState override mechanism
        .def_property("render_state_overrides", &InxMaterial::GetRenderStateOverrides,
                      &InxMaterial::SetRenderStateOverrides,
                      "Bitmask of user-overridden RenderState fields (see RenderStateOverride enum)")
        .def("mark_override", &InxMaterial::MarkOverride, py::arg("flag"),
             "Mark a RenderState field as user-overridden")
        .def("clear_override", &InxMaterial::ClearOverride, py::arg("flag"),
             "Clear a RenderState field override (revert to shader default)")
        .def("has_override", &InxMaterial::HasOverride, py::arg("flag"),
             "Check if a RenderState field is user-overridden")
        .def("sync_alpha_clip_property", &InxMaterial::SyncAlphaClipProperty,
             "Sync internal _AlphaClipThreshold material property from RenderState")
        // Clone / Instantiate (Unity-style Object.Instantiate for materials)
        .def("clone", &InxMaterial::Clone,
             "Create a deep copy of this material (Unity: new Material(original)). "
             "Copies all properties, shader names, and render state. "
             "GPU state is lazily recreated. The clone has no GUID (runtime-only).")
        .def_static(
            "instantiate",
            [](const std::shared_ptr<InxMaterial> &original) -> std::shared_ptr<InxMaterial> {
                if (!original)
                    return nullptr;
                return original->Clone();
            },
            py::arg("original"),
            "Clone a material (Unity: Object.Instantiate). Deep-copies all properties; "
            "shader and texture references are shared. Returns a new runtime material instance.");

    // RenderStateOverride — bitmask flags for per-material render state overrides
    py::enum_<RenderStateOverride>(m, "RenderStateOverride", py::arithmetic())
        .value("NONE", RenderStateOverride::None)
        .value("CULL_MODE", RenderStateOverride::CullMode)
        .value("DEPTH_WRITE", RenderStateOverride::DepthWrite)
        .value("DEPTH_TEST", RenderStateOverride::DepthTest)
        .value("DEPTH_COMPARE_OP", RenderStateOverride::DepthCompareOp)
        .value("BLEND_ENABLE", RenderStateOverride::BlendEnable)
        .value("BLEND_MODE", RenderStateOverride::BlendMode)
        .value("RENDER_QUEUE", RenderStateOverride::RenderQueue)
        .value("SURFACE_TYPE", RenderStateOverride::SurfaceType)
        .value("ALPHA_CLIP", RenderStateOverride::AlphaClip)
        .export_values();

    // RenderState — GPU pipeline state for materials
    py::class_<RenderState>(m, "RenderState")
        .def(py::init<>())
        // Rasterization
        .def_readwrite("cull_mode", &RenderState::cullMode, "VkCullModeFlags: 0=None, 1=Front, 2=Back, 3=FrontAndBack")
        .def_property(
            "front_face", [](const RenderState &rs) { return static_cast<int>(rs.frontFace); },
            [](RenderState &rs, int v) { rs.frontFace = static_cast<VkFrontFace>(v); },
            "VkFrontFace: 0=CounterClockwise, 1=Clockwise")
        .def_property(
            "polygon_mode", [](const RenderState &rs) { return static_cast<int>(rs.polygonMode); },
            [](RenderState &rs, int v) { rs.polygonMode = static_cast<VkPolygonMode>(v); },
            "VkPolygonMode: 0=Fill, 1=Line, 2=Point")
        .def_readwrite("line_width", &RenderState::lineWidth)
        // Depth
        .def_readwrite("depth_test_enable", &RenderState::depthTestEnable)
        .def_readwrite("depth_write_enable", &RenderState::depthWriteEnable)
        .def_property(
            "depth_compare_op", [](const RenderState &rs) { return static_cast<int>(rs.depthCompareOp); },
            [](RenderState &rs, int v) { rs.depthCompareOp = static_cast<VkCompareOp>(v); },
            "VkCompareOp: 0=Never,1=Less,2=Equal,3=LessOrEqual,4=Greater,5=NotEqual,6=GreaterOrEqual,7=Always")
        // Blending
        .def_readwrite("blend_enable", &RenderState::blendEnable)
        .def_property(
            "src_color_blend_factor", [](const RenderState &rs) { return static_cast<int>(rs.srcColorBlendFactor); },
            [](RenderState &rs, int v) { rs.srcColorBlendFactor = static_cast<VkBlendFactor>(v); })
        .def_property(
            "dst_color_blend_factor", [](const RenderState &rs) { return static_cast<int>(rs.dstColorBlendFactor); },
            [](RenderState &rs, int v) { rs.dstColorBlendFactor = static_cast<VkBlendFactor>(v); })
        .def_property(
            "color_blend_op", [](const RenderState &rs) { return static_cast<int>(rs.colorBlendOp); },
            [](RenderState &rs, int v) { rs.colorBlendOp = static_cast<VkBlendOp>(v); })
        .def_property(
            "src_alpha_blend_factor", [](const RenderState &rs) { return static_cast<int>(rs.srcAlphaBlendFactor); },
            [](RenderState &rs, int v) { rs.srcAlphaBlendFactor = static_cast<VkBlendFactor>(v); })
        .def_property(
            "dst_alpha_blend_factor", [](const RenderState &rs) { return static_cast<int>(rs.dstAlphaBlendFactor); },
            [](RenderState &rs, int v) { rs.dstAlphaBlendFactor = static_cast<VkBlendFactor>(v); })
        .def_property(
            "alpha_blend_op", [](const RenderState &rs) { return static_cast<int>(rs.alphaBlendOp); },
            [](RenderState &rs, int v) { rs.alphaBlendOp = static_cast<VkBlendOp>(v); })
        // Render queue
        .def_readwrite("render_queue", &RenderState::renderQueue, "Sorting order: 2000=Opaque, 3000=Transparent")
        // Alpha clip
        .def_readwrite("alpha_clip_enabled", &RenderState::alphaClipEnabled, "Whether alpha clipping is enabled")
        .def_readwrite("alpha_clip_threshold", &RenderState::alphaClipThreshold, "Alpha clip threshold (0.0-1.0)");
}

} // namespace infernux
