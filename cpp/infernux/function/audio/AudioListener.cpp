#include "AudioListener.h"
#include "AudioEngine.h"
#include <core/log/InxLog.h>
#include <function/scene/ComponentDocumentValidation.h>
#include <function/scene/ComponentFactory.h>
#include <function/scene/GameObject.h>

#include <nlohmann/json.hpp>

using json = nlohmann::json;

namespace infernux
{

// Register AudioListener with ComponentFactory
INFERNUX_REGISTER_VALIDATED_COMPONENT("AudioListener", AudioListener)

void AudioListener::Awake()
{
    AudioEngine::Instance().RegisterListener(this);
}

void AudioListener::OnEnable()
{
    AudioEngine::Instance().RegisterListener(this);
}

void AudioListener::OnDisable()
{
    AudioEngine::Instance().UnregisterListener(this);
}

void AudioListener::OnDestroy()
{
    AudioEngine::Instance().UnregisterListener(this);
}

nlohmann::json AudioListener::SerializeDocument() const
{
    return Component::SerializeDocument();
}

void AudioListener::ValidateSerializedDocument(const nlohmann::json &document)
{
    component_document_validation::ValidateComponentDocument(document, "AudioListener", {});
}

bool AudioListener::DeserializeDocument(const nlohmann::json &document)
{
    try {
        ValidateSerializedDocument(document);
    } catch (const std::exception &error) {
        INXLOG_ERROR("AudioListener::Deserialize failed: ", error.what());
        return false;
    }
    return Component::DeserializeDocument(document);
}

uint64_t AudioListener::GetGameObjectId() const
{
    auto *go = GetGameObject();
    return go ? go->GetID() : 0;
}

} // namespace infernux
