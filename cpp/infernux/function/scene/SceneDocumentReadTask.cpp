#include "SceneDocumentReadTask.h"

#include <core/threading/JobSystem.h>
#include <filesystem>
#include <fstream>
#include <function/scene/ComponentFactory.h>
#include <function/scene/ComponentRecord.h>
#include <functional>
#include <stdexcept>
#include <thread>
#include <unordered_map>
#include <unordered_set>

namespace infernux
{
namespace
{

using json = nlohmann::json;

uint64_t CurrentThreadId() noexcept
{
    return static_cast<uint64_t>(std::hash<std::thread::id>{}(std::this_thread::get_id()));
}

void RequireExactFields(const json &document, const std::unordered_set<std::string> &allowed, const std::string &path)
{
    if (!document.is_object())
        throw std::invalid_argument(path + " must be an object");
    for (const auto &[key, value] : document.items()) {
        (void)value;
        if (allowed.find(key) == allowed.end())
            throw std::invalid_argument(path + " contains unknown field '" + key + "'");
    }
}

uint64_t RequirePositiveId(const json &document, const char *field, const std::string &path)
{
    if (!document.contains(field) || !document[field].is_number_unsigned())
        throw std::invalid_argument(path + "." + field + " must be an unsigned integer");
    const uint64_t value = document[field].get<uint64_t>();
    if (value == 0)
        throw std::invalid_argument(path + "." + field + " must be non-zero");
    return value;
}

void ValidateNativeComponentRecord(const json &component, const std::string &path,
                                   std::unordered_set<uint64_t> &componentIds,
                                   std::unordered_map<uint64_t, std::string> &componentTypes)
{
    if (!component.is_object() || !component.contains("type") || !component["type"].is_string() ||
        component["type"].get_ref<const std::string &>().empty() || !component.contains("enabled") ||
        !component["enabled"].is_boolean() || !component.contains("execution_order") ||
        !component["execution_order"].is_number_integer()) {
        throw std::invalid_argument(path + " has invalid common component fields");
    }
    const std::string type = component["type"].get<std::string>();
    if (!ComponentFactory::IsRegistered(type))
        throw std::invalid_argument(path + " references unregistered native component type '" + type + "'");
    try {
        ComponentFactory::ValidateDocument(type, component);
    } catch (const std::exception &error) {
        throw std::invalid_argument(path + ": " + error.what());
    }

    const uint64_t id = RequirePositiveId(component, "component_id", path);
    if (!componentIds.insert(id).second)
        throw std::invalid_argument(path + " duplicates component_id " + std::to_string(id));
    componentTypes.emplace(id, type);
}

void ValidateUnifiedComponentRecord(const json &component, const std::string &path,
                                    std::unordered_set<uint64_t> &componentIds,
                                    std::unordered_map<uint64_t, std::string> &componentTypes)
{
    DecodedComponentRecord record;
    try {
        record = DecodeComponentRecord(component);
    } catch (const std::exception &error) {
        throw std::invalid_argument(path + ": " + error.what());
    }
    if (!componentIds.insert(record.componentId).second)
        throw std::invalid_argument(path + " duplicates component_id " + std::to_string(record.componentId));
    if (record.kind == ComponentRecordKind::Python)
        return;
    if (!ComponentFactory::IsRegistered(record.nativeTypeName)) {
        throw std::invalid_argument(path + " references unregistered native component type '" + record.nativeTypeName +
                                    "'");
    }
    try {
        ComponentFactory::ValidateDocument(record.nativeTypeName, BuildNativeComponentDocument(record));
    } catch (const std::exception &error) {
        throw std::invalid_argument(path + ": " + error.what());
    }
    componentTypes.emplace(record.componentId, record.nativeTypeName);
}

void ValidateObject(const json &object, const std::string &path, std::unordered_set<uint64_t> &objectIds,
                    std::unordered_set<uint64_t> &componentIds,
                    std::unordered_map<uint64_t, std::string> &componentTypes)
{
    static const std::unordered_set<std::string> allowed = {
        "name",        "id",          "active",    "is_static",  "tag",      "layer",
        "prefab_guid", "prefab_root", "transform", "components", "children",
    };
    RequireExactFields(object, allowed, path);
    if (!object.contains("name") || !object["name"].is_string() || !object.contains("active") ||
        !object["active"].is_boolean() || !object.contains("is_static") || !object["is_static"].is_boolean() ||
        !object.contains("tag") || !object["tag"].is_string() || !object.contains("layer") ||
        !object["layer"].is_number_integer() || !object.contains("transform") || !object.contains("components") ||
        !object["components"].is_array() || !object.contains("children") || !object["children"].is_array()) {
        throw std::invalid_argument(path + " has invalid GameObject fields");
    }
    const int layer = object["layer"].get<int>();
    if (layer < 0 || layer >= 32)
        throw std::invalid_argument(path + ".layer must be in [0, 31]");
    if (object.contains("prefab_guid") && !object["prefab_guid"].is_string())
        throw std::invalid_argument(path + ".prefab_guid must be a string");
    if (object.contains("prefab_root") && !object["prefab_root"].is_boolean())
        throw std::invalid_argument(path + ".prefab_root must be boolean");

    const uint64_t objectId = RequirePositiveId(object, "id", path);
    if (!objectIds.insert(objectId).second)
        throw std::invalid_argument(path + " duplicates GameObject id " + std::to_string(objectId));

    ValidateNativeComponentRecord(object["transform"], path + ".transform", componentIds, componentTypes);
    if (object["transform"].value("type", std::string()) != "Transform")
        throw std::invalid_argument(path + ".transform must have type Transform");
    for (size_t index = 0; index < object["components"].size(); ++index)
        ValidateUnifiedComponentRecord(object["components"][index], path + ".components[" + std::to_string(index) + "]",
                                       componentIds, componentTypes);
    for (size_t index = 0; index < object["children"].size(); ++index)
        ValidateObject(object["children"][index], path + ".children[" + std::to_string(index) + "]", objectIds,
                       componentIds, componentTypes);
}

void ValidateSceneDocument(const json &document)
{
    static const std::unordered_set<std::string> allowed = {
        "name", "isPlaying", "objects", "mainCameraComponentId", "environment",
    };
    RequireExactFields(document, allowed, "Scene");
    if (!document.contains("name") || !document["name"].is_string() || !document.contains("isPlaying") ||
        !document["isPlaying"].is_boolean() || !document.contains("objects") || !document["objects"].is_array()) {
        throw std::invalid_argument("Scene has invalid required fields");
    }

    std::unordered_set<uint64_t> objectIds;
    std::unordered_set<uint64_t> componentIds;
    std::unordered_map<uint64_t, std::string> componentTypes;
    for (size_t index = 0; index < document["objects"].size(); ++index)
        ValidateObject(document["objects"][index], "Scene.objects[" + std::to_string(index) + "]", objectIds,
                       componentIds, componentTypes);

    if (document.contains("mainCameraComponentId")) {
        const uint64_t cameraId = RequirePositiveId(document, "mainCameraComponentId", "Scene");
        const auto camera = componentTypes.find(cameraId);
        if (camera == componentTypes.end() || camera->second != "Camera")
            throw std::invalid_argument("Scene.mainCameraComponentId must reference a native Camera");
    }
}

std::string ReadFile(const std::string &path)
{
    std::ifstream input(std::filesystem::u8path(path), std::ios::binary);
    if (!input)
        throw std::runtime_error("failed to open scene file: " + path);
    input.seekg(0, std::ios::end);
    const auto size = input.tellg();
    if (size < 0)
        throw std::runtime_error("failed to measure scene file: " + path);
    std::string bytes(static_cast<size_t>(size), '\0');
    input.seekg(0, std::ios::beg);
    if (!bytes.empty() && !input.read(bytes.data(), static_cast<std::streamsize>(bytes.size())))
        throw std::runtime_error("failed to read complete scene file: " + path);
    return bytes;
}

} // namespace

bool SceneDocumentReadTicket::IsComplete() const noexcept
{
    if (!m_state)
        return true;
    return m_state->status.load(std::memory_order_acquire) != Status::Pending;
}

bool SceneDocumentReadTicket::IsReady() const noexcept
{
    return m_state && m_state->status.load(std::memory_order_acquire) == Status::Ready;
}

bool SceneDocumentReadTicket::RanOnWorker() const noexcept
{
    if (!m_state)
        return false;
    const uint64_t workerThread = m_state->workerThread.load(std::memory_order_acquire);
    return workerThread != 0 && workerThread != m_state->callerThread.load(std::memory_order_acquire);
}

std::string SceneDocumentReadTicket::GetStatusName() const
{
    if (!m_state)
        return "invalid";
    switch (m_state->status.load(std::memory_order_acquire)) {
    case Status::Pending:
        return "pending";
    case Status::Ready:
        return "ready";
    case Status::Failed:
        return "failed";
    case Status::Cancelled:
        return "cancelled";
    case Status::Consumed:
        return "consumed";
    }
    return "invalid";
}

std::string SceneDocumentReadTicket::GetError() const
{
    if (!m_state)
        return "invalid scene document ticket";
    std::lock_guard<std::mutex> lock(m_state->mutex);
    return m_state->error;
}

bool SceneDocumentReadTicket::Cancel()
{
    if (!m_state)
        return false;
    std::lock_guard<std::mutex> lock(m_state->mutex);
    const Status status = m_state->status.load(std::memory_order_acquire);
    if (status == Status::Pending) {
        m_state->cancelRequested.store(true, std::memory_order_release);
        return true;
    }
    if (status == Status::Ready) {
        m_state->document = json();
        m_state->status.store(Status::Cancelled, std::memory_order_release);
        return true;
    }
    return false;
}

nlohmann::json SceneDocumentReadTicket::TakeDocument()
{
    if (!m_state)
        throw std::logic_error("invalid scene document ticket");
    std::lock_guard<std::mutex> lock(m_state->mutex);
    if (m_state->status.load(std::memory_order_acquire) != Status::Ready)
        throw std::logic_error("scene document ticket is not ready");
    json result = std::move(m_state->document);
    m_state->status.store(Status::Consumed, std::memory_order_release);
    return result;
}

SceneDocumentReadTicket ScheduleSceneDocumentRead(const std::string &path)
{
    if (path.empty())
        throw std::invalid_argument("scene document path must be non-empty");
    if (!JobSystem::IsAvailable())
        throw std::logic_error("scene document read requires the engine JobSystem");

    auto state = std::make_shared<SceneDocumentReadTicket::State>();
    state->callerThread = CurrentThreadId();
    JobSystem::Get().Schedule([state, path] {
        state->workerThread = CurrentThreadId();
        try {
            if (state->cancelRequested.load(std::memory_order_acquire)) {
                state->status.store(SceneDocumentReadTicket::Status::Cancelled, std::memory_order_release);
                return;
            }
            json document = json::parse(ReadFile(path));
            ValidateSceneDocument(document);
            std::lock_guard<std::mutex> lock(state->mutex);
            if (state->cancelRequested.load(std::memory_order_acquire)) {
                state->status.store(SceneDocumentReadTicket::Status::Cancelled, std::memory_order_release);
                return;
            }
            state->document = std::move(document);
            state->status.store(SceneDocumentReadTicket::Status::Ready, std::memory_order_release);
        } catch (const std::exception &error) {
            std::lock_guard<std::mutex> lock(state->mutex);
            if (state->cancelRequested.load(std::memory_order_acquire)) {
                state->status.store(SceneDocumentReadTicket::Status::Cancelled, std::memory_order_release);
                return;
            }
            state->error = error.what();
            state->status.store(SceneDocumentReadTicket::Status::Failed, std::memory_order_release);
        }
    });
    return SceneDocumentReadTicket(std::move(state));
}

} // namespace infernux
