#pragma once

#include <core/types/InxFwdType.h>
#include <function/resources/InxResource/InxResourceMeta.h>

#include <cstddef>
#include <memory>
#include <set>
#include <stdexcept>
#include <string>
#include <typeindex>
#include <utility>

namespace infernux
{

class AssetDatabase;

class RuntimeAssetPayload final
{
  public:
    RuntimeAssetPayload() = default;
    RuntimeAssetPayload(std::nullptr_t) noexcept
    {
    }

    template <typename T>
    RuntimeAssetPayload(std::shared_ptr<T> instance) : m_instance(std::move(instance)), m_type(typeid(T))
    {
    }

    [[nodiscard]] explicit operator bool() const noexcept
    {
        return static_cast<bool>(m_instance);
    }

    [[nodiscard]] std::type_index GetType() const noexcept
    {
        return m_type;
    }

    [[nodiscard]] const char *GetTypeName() const noexcept
    {
        return m_type.name();
    }

    [[nodiscard]] long GetUseCount() const noexcept
    {
        return m_instance.use_count();
    }

    template <typename T> [[nodiscard]] std::shared_ptr<T> Get() const
    {
        if (!m_instance)
            return {};
        if (m_type != std::type_index(typeid(T)))
            throw std::invalid_argument(std::string("Runtime asset type mismatch: stored '") + m_type.name() +
                                        "', requested '" + typeid(T).name() + "'");
        return std::static_pointer_cast<T>(m_instance);
    }

  private:
    std::shared_ptr<void> m_instance;
    std::type_index m_type{typeid(void)};
};

/// @brief Interface for type-specific asset loading/reloading in AssetRegistry.
///
/// ── Architecture Note ──────────────────────────────────────────────
/// This is the *runtime loading* layer of the asset pipeline.
/// IAssetLoader turns already-imported assets into live in-memory objects.
///
/// The *import strategy* layer lives in AssetImporter/ (AssetImporter.h).
/// That layer handles source-file processing and .meta generation.
///
/// See AssetImporter.h for the full two-layer pipeline description.
/// ──────────────────────────────────────────────────────────────────
///
/// Each ResourceType registers one IAssetLoader implementation.
/// AssetRegistry delegates Load / Reload / ScanDependencies to the loader.
class IAssetLoader
{
  public:
    virtual ~IAssetLoader() = default;

    /// @brief Load an asset from disk.
    /// @return Type-tagged runtime object. Empty means this asset has no C++ runtime payload.
    virtual RuntimeAssetPayload Load(const std::string &filePath, const std::string &guid, AssetDatabase *adb) = 0;

    /// Worker loading is opt-in. Implementations may only read immutable state.
    [[nodiscard]] virtual bool SupportsWorkerLoad() const noexcept
    {
        return false;
    }

    /// Load transient data needed to publish a runtime resource. The returned
    /// payload must never be inserted into AssetRegistry; the consumer owns it
    /// only until it has copied the data into its native staging resource.
    virtual RuntimeAssetPayload LoadStaging(const std::string & /*filePath*/, const std::string & /*guid*/,
                                            AssetDatabase * /*adb*/)
    {
        return nullptr;
    }

    /// @brief Reload an already-loaded asset in-place.
    /// @return true on success.
    virtual bool Reload(const RuntimeAssetPayload &existing, const std::string &filePath, const std::string &guid,
                        AssetDatabase *adb) = 0;

    /// @brief Estimate bytes exclusively owned by this live CPU payload.
    /// A non-empty payload must never report zero.
    [[nodiscard]] virtual size_t EstimateRuntimeBytes(const RuntimeAssetPayload &payload) const = 0;

    /// @brief Return GUIDs of assets this asset depends on.
    ///
    /// Durable dependency identity is GUID-only. Implementations must never
    /// derive an edge from a path, path_hint, filename, or search result.
    virtual std::set<std::string> ScanDependencies(const std::string &filePath, AssetDatabase *adb) = 0;

    /// @brief Create metadata from the source bytes already read by AssetDatabase.
    virtual void CreateMeta(const char * /*content*/, size_t /*contentSize*/, const std::string & /*filePath*/,
                            InxResourceMeta & /*metaData*/) const
    {
    }
};

} // namespace infernux
