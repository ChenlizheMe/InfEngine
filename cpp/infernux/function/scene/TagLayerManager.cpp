/**
 * @file TagLayerManager.cpp
 * @brief Implementation of the Tag and Layer management system.
 */

#include "TagLayerManager.h"
#include "platform/filesystem/DocumentStore.h"
#include <algorithm>
#include <array>
#include <core/log/InxLog.h>
#include <fstream>
#include <nlohmann/json.hpp>
#include <platform/filesystem/InxPath.h>
#include <unordered_set>

using json = nlohmann::json;

namespace infernux
{

const std::string TagLayerManager::s_emptyString;

TagLayerManager &TagLayerManager::Instance()
{
    // Intentionally leaked (see SceneManager::Instance).
    static TagLayerManager *instance = new TagLayerManager();
    return *instance;
}

TagLayerManager::TagLayerManager()
{
    InitDefaults();
}

void TagLayerManager::InitDefaults()
{
    // Built-in tags (indices 0-6)
    m_tags.clear();
    m_tags.push_back("Untagged");       // 0
    m_tags.push_back("Respawn");        // 1
    m_tags.push_back("Finish");         // 2
    m_tags.push_back("EditorOnly");     // 3
    m_tags.push_back("MainCamera");     // 4
    m_tags.push_back("Player");         // 5
    m_tags.push_back("GameController"); // 6

    // 32 layers, built-in names for certain indices
    m_layers.resize(kMaxLayers);
    m_layers[0] = "Default";
    m_layers[1] = "TransparentFX";
    m_layers[2] = "IgnoreRaycast";
    m_layers[3] = "";
    m_layers[4] = "Water";
    m_layers[5] = "UI";
    for (int i = 6; i < kMaxLayers; ++i) {
        m_layers[i] = "";
    }

    // Default Unity-like behaviour: all layers collide with all layers.
    m_layerCollisionMasks.resize(kMaxLayers);
    for (int i = 0; i < kMaxLayers; ++i) {
        m_layerCollisionMasks[i] = 0xFFFFFFFFu;
    }
}

// ============================================================================
// Tags
// ============================================================================

const std::string &TagLayerManager::GetTag(int index) const
{
    if (index >= 0 && index < static_cast<int>(m_tags.size())) {
        return m_tags[index];
    }
    return s_emptyString;
}

int TagLayerManager::GetTagIndex(const std::string &tag) const
{
    for (int i = 0; i < static_cast<int>(m_tags.size()); ++i) {
        if (m_tags[i] == tag)
            return i;
    }
    return -1;
}

int TagLayerManager::AddTag(const std::string &tag)
{
    if (tag.empty()) {
        INXLOG_WARN("TagLayerManager::AddTag: cannot add empty tag");
        return -1;
    }
    // Check for duplicates
    int existing = GetTagIndex(tag);
    if (existing >= 0) {
        return existing;
    }
    m_tags.push_back(tag);
    INXLOG_DEBUG("TagLayerManager::AddTag: added tag '", tag, "' at index ", m_tags.size() - 1);
    return static_cast<int>(m_tags.size()) - 1;
}

bool TagLayerManager::RemoveTag(const std::string &tag)
{
    if (IsBuiltinTag(tag)) {
        INXLOG_WARN("TagLayerManager::RemoveTag: cannot remove built-in tag '", tag, "'");
        return false;
    }
    int idx = GetTagIndex(tag);
    if (idx < 0) {
        return false;
    }
    m_tags.erase(m_tags.begin() + idx);
    INXLOG_DEBUG("TagLayerManager::RemoveTag: removed tag '", tag, "'");
    return true;
}

const std::vector<std::string> &TagLayerManager::GetAllTags() const
{
    return m_tags;
}

bool TagLayerManager::IsBuiltinTag(const std::string &tag) const
{
    int idx = GetTagIndex(tag);
    return idx >= 0 && idx < kBuiltinTagCount;
}

// ============================================================================
// Layers
// ============================================================================

const std::string &TagLayerManager::GetLayerName(int layer) const
{
    if (layer >= 0 && layer < kMaxLayers) {
        return m_layers[layer];
    }
    return s_emptyString;
}

int TagLayerManager::GetLayerByName(const std::string &name) const
{
    if (name.empty())
        return -1;
    for (int i = 0; i < kMaxLayers; ++i) {
        if (m_layers[i] == name)
            return i;
    }
    return -1;
}

bool TagLayerManager::SetLayerName(int layer, const std::string &name)
{
    if (layer < 0 || layer >= kMaxLayers) {
        INXLOG_WARN("TagLayerManager::SetLayerName: invalid layer index ", layer);
        return false;
    }
    if (IsBuiltinLayer(layer)) {
        INXLOG_WARN("TagLayerManager::SetLayerName: cannot rename built-in layer ", layer, " ('", m_layers[layer],
                    "')");
        return false;
    }
    m_layers[layer] = name;
    INXLOG_DEBUG("TagLayerManager::SetLayerName: layer ", layer, " = '", name, "'");
    return true;
}

const std::vector<std::string> &TagLayerManager::GetAllLayers() const
{
    return m_layers;
}

bool TagLayerManager::IsBuiltinLayer(int layer) const
{
    // Built-in layers: 0 (Default), 1 (TransparentFX), 2 (IgnoreRaycast), 4 (Water), 5 (UI)
    return layer == 0 || layer == 1 || layer == 2 || layer == 4 || layer == 5;
}

uint32_t TagLayerManager::GetLayerCollisionMask(int layer) const
{
    if (layer < 0 || layer >= kMaxLayers) {
        return 0;
    }
    return m_layerCollisionMasks[layer];
}

bool TagLayerManager::SetLayerCollisionMask(int layer, uint32_t mask)
{
    if (layer < 0 || layer >= kMaxLayers) {
        INXLOG_WARN("TagLayerManager::SetLayerCollisionMask: invalid layer index ", layer);
        return false;
    }

    m_layerCollisionMasks[layer] = mask;

    // Keep the matrix symmetric.
    for (int other = 0; other < kMaxLayers; ++other) {
        bool enabled = (mask & LayerToMask(other)) != 0;
        if (enabled)
            m_layerCollisionMasks[other] |= LayerToMask(layer);
        else
            m_layerCollisionMasks[other] &= ~LayerToMask(layer);
    }
    return true;
}

bool TagLayerManager::GetLayersCollide(int layerA, int layerB) const
{
    if (layerA < 0 || layerA >= kMaxLayers || layerB < 0 || layerB >= kMaxLayers) {
        return false;
    }
    return (m_layerCollisionMasks[layerA] & LayerToMask(layerB)) != 0;
}

bool TagLayerManager::SetLayersCollide(int layerA, int layerB, bool shouldCollide)
{
    if (layerA < 0 || layerA >= kMaxLayers || layerB < 0 || layerB >= kMaxLayers) {
        INXLOG_WARN("TagLayerManager::SetLayersCollide: invalid layer pair ", layerA, ", ", layerB);
        return false;
    }

    const uint32_t maskA = LayerToMask(layerA);
    const uint32_t maskB = LayerToMask(layerB);
    if (shouldCollide) {
        m_layerCollisionMasks[layerA] |= maskB;
        m_layerCollisionMasks[layerB] |= maskA;
    } else {
        m_layerCollisionMasks[layerA] &= ~maskB;
        m_layerCollisionMasks[layerB] &= ~maskA;
    }
    return true;
}

// ============================================================================
// Layer mask helpers
// ============================================================================

uint32_t TagLayerManager::LayerToMask(int layer)
{
    if (layer < 0 || layer >= kMaxLayers)
        return 0;
    return 1u << static_cast<uint32_t>(layer);
}

uint32_t TagLayerManager::GetMask(const std::vector<std::string> &layerNames) const
{
    uint32_t mask = 0;
    for (const auto &name : layerNames) {
        int idx = GetLayerByName(name);
        if (idx >= 0) {
            mask |= LayerToMask(idx);
        }
    }
    return mask;
}

// ============================================================================
// Serialization
// ============================================================================

std::string TagLayerManager::Serialize() const
{
    json j;
    // Only serialize custom tags (indices >= kBuiltinTagCount)
    json customTags = json::array();
    for (int i = kBuiltinTagCount; i < static_cast<int>(m_tags.size()); ++i) {
        customTags.push_back(m_tags[i]);
    }
    j["custom_tags"] = customTags;

    // Serialize all 32 layers (built-in + custom names)
    json layers = json::array();
    for (int i = 0; i < kMaxLayers; ++i) {
        layers.push_back(m_layers[i]);
    }
    j["layers"] = layers;

    json collisionMasks = json::array();
    for (int i = 0; i < kMaxLayers; ++i) {
        collisionMasks.push_back(m_layerCollisionMasks[i]);
    }
    j["layer_collision_masks"] = collisionMasks;

    return j.dump(2);
}

bool TagLayerManager::Deserialize(const std::string &jsonStr)
{
    try {
        json j = json::parse(jsonStr);
        if (!j.is_object() || j.size() != 3 || !j.contains("custom_tags") || !j["custom_tags"].is_array() ||
            !j.contains("layers") || !j["layers"].is_array() || !j.contains("layer_collision_masks") ||
            !j["layer_collision_masks"].is_array()) {
            throw std::invalid_argument("expected the complete TagLayerSettings document");
        }

        static const std::unordered_set<std::string> builtinTags = {
            "Untagged", "Respawn", "Finish", "EditorOnly", "MainCamera", "Player", "GameController"};
        std::vector<std::string> customTags;
        std::unordered_set<std::string> seenTags = builtinTags;
        for (const auto &tag : j["custom_tags"]) {
            if (!tag.is_string())
                throw std::invalid_argument("custom_tags must contain strings");
            std::string value = tag.get<std::string>();
            if (value.empty() || !seenTags.insert(value).second)
                throw std::invalid_argument("custom_tags must be non-empty and unique");
            customTags.push_back(std::move(value));
        }

        if (j["layers"].size() != kMaxLayers)
            throw std::invalid_argument("layers must contain exactly 32 strings");
        std::vector<std::string> layers;
        layers.reserve(kMaxLayers);
        for (const auto &layer : j["layers"]) {
            if (!layer.is_string())
                throw std::invalid_argument("layers must contain exactly 32 strings");
            layers.push_back(layer.get<std::string>());
        }
        static const std::array<std::pair<int, const char *>, 5> builtinLayers = {
            std::pair{0, "Default"}, std::pair{1, "TransparentFX"}, std::pair{2, "IgnoreRaycast"},
            std::pair{4, "Water"}, std::pair{5, "UI"}};
        for (const auto &[index, expectedName] : builtinLayers) {
            if (layers[index] != expectedName)
                throw std::invalid_argument("built-in layer names cannot be changed");
        }

        if (j["layer_collision_masks"].size() != kMaxLayers)
            throw std::invalid_argument("layer_collision_masks must contain exactly 32 unsigned integers");
        std::vector<uint32_t> collisionMasks;
        collisionMasks.reserve(kMaxLayers);
        for (const auto &mask : j["layer_collision_masks"]) {
            if (!mask.is_number_unsigned())
                throw std::invalid_argument("layer_collision_masks must contain exactly 32 unsigned integers");
            collisionMasks.push_back(mask.get<uint32_t>());
        }
        for (int a = 0; a < kMaxLayers; ++a) {
            for (int b = a + 1; b < kMaxLayers; ++b) {
                const bool ab = (collisionMasks[a] & LayerToMask(b)) != 0;
                const bool ba = (collisionMasks[b] & LayerToMask(a)) != 0;
                if (ab != ba)
                    throw std::invalid_argument("layer collision matrix must be symmetric");
            }
        }

        InitDefaults();
        m_tags.insert(m_tags.end(), customTags.begin(), customTags.end());
        for (int i = 0; i < kMaxLayers; ++i) {
            if (!IsBuiltinLayer(i))
                m_layers[i] = std::move(layers[i]);
        }
        m_layerCollisionMasks = std::move(collisionMasks);

        INXLOG_DEBUG("TagLayerManager: deserialized ", m_tags.size() - kBuiltinTagCount, " custom tags");
        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("TagLayerManager::Deserialize failed: ", e.what());
        return false;
    }
}

bool TagLayerManager::SaveToFile(const std::string &path) const
{
    try {
        std::string jsonStr = Serialize();
        DocumentStore::Instance().WriteAndWait(path, std::move(jsonStr));
        INXLOG_INFO("TagLayerManager: saved to '", path, "'");
        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("TagLayerManager::SaveToFile failed: ", e.what());
        return false;
    }
}

bool TagLayerManager::LoadFromFile(const std::string &path)
{
    try {
        std::ifstream file = OpenInputFile(path);
        if (!file.is_open()) {
            INXLOG_DEBUG("TagLayerManager::LoadFromFile: file not found '", path, "', using defaults");
            return false;
        }
        std::string jsonStr((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
        file.close();
        return Deserialize(jsonStr);
    } catch (const std::exception &e) {
        INXLOG_ERROR("TagLayerManager::LoadFromFile failed: ", e.what());
        return false;
    }
}

} // namespace infernux
