#pragma once

#include <array>
#include <core/types/InxFwdType.h>
#include <core/types/ShaderAssetReference.h>
#include <core/types/ShaderTypes.h>
#include <cstdint>
#include <glm/glm.hpp>
#include <memory>
#include <nlohmann/json.hpp>
#include <optional>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <variant>
#include <vector>
#include <vk_mem_alloc.h>
#include <vulkan/vulkan.h>

namespace infernux
{

// Forward declarations
class MeshRenderer;
class ShaderProgram;
struct MaterialUBOLayout;

/**
 * @brief Shader stage type for the material system
 */
enum class ShaderStageType
{
    Vertex,
    Fragment,
    Geometry,
    TessControl,
    TessEval
};

/**
 * @brief Bitmask flags indicating which RenderState fields have been
 *        manually overridden by the user via the Material Inspector.
 *
 * When ApplyShaderRenderMeta() is called (shader annotation defaults),
 * only fields whose corresponding override bit is NOT set will be updated.
 * This allows per-material customization that survives shader reloads.
 */
enum class RenderStateOverride : uint32_t
{
    None = 0,
    CullMode = 1 << 0,
    DepthWrite = 1 << 1,
    DepthTest = 1 << 2,
    DepthCompareOp = 1 << 3,
    BlendEnable = 1 << 4,
    BlendMode = 1 << 5,
    RenderQueue = 1 << 6,
    SurfaceType = 1 << 7,
    AlphaClip = 1 << 8,
};

/**
 * @brief Render state configuration for materials
 *
 * This defines how the GPU should render geometry with this material.
 */
struct RenderState
{
    // Rasterization
    VkCullModeFlags cullMode = VK_CULL_MODE_BACK_BIT;
    VkFrontFace frontFace = VK_FRONT_FACE_CLOCKWISE;
    VkPolygonMode polygonMode = VK_POLYGON_MODE_FILL;
    float lineWidth = 1.0f;

    // Depth bias (polygon offset) — pushes fragments in depth to avoid z-fighting
    bool depthBiasEnable = false;
    float depthBiasConstantFactor = 0.0f;
    float depthBiasSlopeFactor = 0.0f;
    float depthBiasClamp = 0.0f;

    // Primitive topology (default: triangle list)
    VkPrimitiveTopology topology = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST;

    // Depth/Stencil
    bool depthTestEnable = true;
    bool depthWriteEnable = true;
    VkCompareOp depthCompareOp = VK_COMPARE_OP_LESS;
    bool stencilTestEnable = false;
    VkStencilOpState stencilFront{}; // front-face stencil operations
    VkStencilOpState stencilBack{};  // back-face stencil operations

    // Blending
    bool blendEnable = false;
    VkBlendFactor srcColorBlendFactor = VK_BLEND_FACTOR_SRC_ALPHA;
    VkBlendFactor dstColorBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
    VkBlendOp colorBlendOp = VK_BLEND_OP_ADD;
    VkBlendFactor srcAlphaBlendFactor = VK_BLEND_FACTOR_ZERO;
    VkBlendFactor dstAlphaBlendFactor = VK_BLEND_FACTOR_ONE;
    VkBlendOp alphaBlendOp = VK_BLEND_OP_ADD;

    // Alpha clip (runtime toggle — controls _AlphaClipThreshold material property)
    bool alphaClipEnabled = false;
    float alphaClipThreshold = 0.5f;

    // Render queue (for sorting)
    int32_t renderQueue = 2000; // 2000 = Opaque, 3000 = Transparent

    bool operator==(const RenderState &other) const;
    size_t Hash() const;
};

/**
 * @brief Material property types
 */
enum class MaterialPropertyType
{
    Float,
    Float2,
    Float3,
    Float4,
    Int,
    Mat4,
    Texture2D,
    Color // = 7: vec4 colour, identical storage to Float4
};

/**
 * @brief A single material property value
 */
using MaterialPropertyValue = std::variant<float, glm::vec2, glm::vec3, glm::vec4, int, glm::mat4, std::string>;

/**
 * @brief Material property descriptor
 */
struct MaterialProperty
{
    std::string name;
    MaterialPropertyType type;
    MaterialPropertyValue value;
    bool hdr = false;
    std::optional<std::array<double, 2>> range;
};

/**
 * @brief InxMaterial - Material definition for rendering
 *
 * A material in Infernux consists of:
 * - A shader name (e.g. "lit") identifying all pass variants
 * - Render state configuration
 * - Material properties (uniforms, textures)
 * - Per-pass pipeline storage (Forward, GBuffer, Shadow)
 */
class InxMaterial
{
  public:
    struct DetachedUBO
    {
        VmaAllocator allocator = VK_NULL_HANDLE;
        VkBuffer buffer = VK_NULL_HANDLE;
        VmaAllocation allocation = VK_NULL_HANDLE;
    };
    InxMaterial() = default;
    InxMaterial(const std::string &name);
    InxMaterial(const std::string &name, const std::string &shaderName);
    ~InxMaterial() = default;

    // Copying creates a distinct runtime material identity.
    InxMaterial(const InxMaterial &other);
    InxMaterial &operator=(const InxMaterial &other);

    // ========================================================================
    // Identity
    // ========================================================================

    [[nodiscard]] const std::string &GetName() const
    {
        return m_name;
    }
    void SetName(const std::string &name)
    {
        m_name = name;
    }

    [[nodiscard]] const std::string &GetGuid() const
    {
        return m_guid;
    }

    [[nodiscard]] const std::string &GetFilePath() const
    {
        return m_filePath;
    }
    void SetFilePath(const std::string &path)
    {
        m_filePath = path;
    }

    // ========================================================================
    // Deleted flag — set when the backing .mat file is removed from disk.
    // A deleted material should not be rendered or saved.
    // ========================================================================

    [[nodiscard]] bool IsDeleted() const
    {
        return m_isDeleted;
    }
    void MarkAsDeleted()
    {
        m_isDeleted = true;
    }

    // ========================================================================
    // Built-in flag (built-in materials cannot have their shader changed)
    // ========================================================================

    [[nodiscard]] bool IsBuiltin() const
    {
        return m_builtin;
    }
    void SetBuiltin(bool builtin)
    {
        m_builtin = builtin;
    }

    /// @brief Save material to its file path (if set)
    bool SaveToFile() const;

    /// @brief Save material to specified file path
    bool SaveToFile(const std::string &path);

    // ========================================================================
    // Shader identity
    // ========================================================================

    /// @brief Set both vertex and fragment shader to the same name (convenience).
    void SetShader(const std::string &shaderName)
    {
        const ShaderAssetReference reference{"", shaderName, ""};
        if (m_vertexShader == reference && m_fragmentShader == reference)
            return;
        m_vertexShader = reference;
        m_fragmentShader = reference;
        m_pipelineDirty = true;
        ++m_version;
    }

    /// @brief Set vertex shader name independently.
    void SetVertShader(const std::string &name)
    {
        const ShaderAssetReference reference{"", name, ""};
        if (m_vertexShader == reference)
            return;
        m_vertexShader = reference;
        m_pipelineDirty = true;
        ++m_version;
    }

    /// @brief Set fragment shader name independently.
    void SetFragShader(const std::string &name)
    {
        const ShaderAssetReference reference{"", name, ""};
        if (m_fragmentShader == reference)
            return;
        m_fragmentShader = reference;
        m_pipelineDirty = true;
        ++m_version;
    }

    void SetVertShaderReference(ShaderAssetReference reference)
    {
        if (m_vertexShader == reference)
            return;
        m_vertexShader = std::move(reference);
        m_pipelineDirty = true;
        ++m_version;
    }

    void SetFragShaderReference(ShaderAssetReference reference)
    {
        if (m_fragmentShader == reference)
            return;
        m_fragmentShader = std::move(reference);
        m_pipelineDirty = true;
        ++m_version;
    }

    /// @brief Get the fragment shader name (primary identity for render meta).
    [[nodiscard]] const std::string &GetShaderName() const
    {
        return m_fragmentShader.shaderId;
    }

    /// @brief Get vertex shader name.
    [[nodiscard]] const std::string &GetVertShaderName() const
    {
        return m_vertexShader.shaderId;
    }

    /// @brief Get fragment shader name.
    [[nodiscard]] const std::string &GetFragShaderName() const
    {
        return m_fragmentShader.shaderId;
    }

    [[nodiscard]] const ShaderAssetReference &GetVertShaderReference() const noexcept
    {
        return m_vertexShader;
    }

    [[nodiscard]] const ShaderAssetReference &GetFragShaderReference() const noexcept
    {
        return m_fragmentShader;
    }

    // ========================================================================
    // Render State
    // ========================================================================

    [[nodiscard]] const RenderState &GetRenderState() const
    {
        return m_renderState;
    }
    void SetRenderState(const RenderState &state)
    {
        if (m_renderState == state)
            return;
        m_renderState = state;
        m_pipelineDirty = true;
        ++m_version;
    }

    [[nodiscard]] int32_t GetRenderQueue() const
    {
        return m_renderState.renderQueue;
    }
    void SetRenderQueue(int32_t queue)
    {
        if (m_renderState.renderQueue == queue)
            return;
        m_renderState.renderQueue = queue;
        ++m_version;
    }

    [[nodiscard]] const std::string &GetPassTag() const
    {
        return m_passTag;
    }
    void SetPassTag(const std::string &tag)
    {
        m_passTag = tag;
    }

    /// @brief Apply shader render-state annotations to this material.
    /// Shader annotations (@cull, @depth_write, @depth_test, @blend, @queue, @pass_tag, @alpha_clip)
    /// set default RenderState values only for fields NOT manually overridden.
    void ApplyShaderRenderMeta(const std::string &cullMode, const std::string &depthWrite, const std::string &depthTest,
                               const std::string &blend, int queue, const std::string &passTag = "",
                               const std::string &stencil = "", const std::string &alphaClip = "");

    /// @brief Sync the internal _AlphaClipThreshold material property from the RenderState.
    /// Must be called after any change to alphaClipEnabled / alphaClipThreshold.
    void SyncAlphaClipProperty();

    // ========================================================================
    // RenderState Override Mechanism
    // ========================================================================

    /// @brief Get the current override bitmask.
    [[nodiscard]] uint32_t GetRenderStateOverrides() const
    {
        return m_renderStateOverrides;
    }

    /// @brief Set the entire override bitmask.
    void SetRenderStateOverrides(uint32_t overrides)
    {
        m_renderStateOverrides = overrides;
    }

    /// @brief Mark a specific render-state field as user-overridden.
    void MarkOverride(RenderStateOverride flag)
    {
        m_renderStateOverrides |= static_cast<uint32_t>(flag);
        m_pipelineDirty = true;
    }

    /// @brief Clear a specific override (revert to shader default on next apply).
    void ClearOverride(RenderStateOverride flag)
    {
        m_renderStateOverrides &= ~static_cast<uint32_t>(flag);
        m_pipelineDirty = true;
    }

    /// @brief Check if a specific field is user-overridden.
    [[nodiscard]] bool HasOverride(RenderStateOverride flag) const
    {
        return (m_renderStateOverrides & static_cast<uint32_t>(flag)) != 0;
    }

    // ========================================================================
    // Material Properties
    // ========================================================================

    void SetFloat(const std::string &name, float value);
    void SetVector2(const std::string &name, const glm::vec2 &value);
    void SetVector3(const std::string &name, const glm::vec3 &value);
    void SetVector4(const std::string &name, const glm::vec4 &value);
    void SetColor(const std::string &name, const glm::vec4 &color);
    void SetInt(const std::string &name, int value);
    void SetMatrix(const std::string &name, const glm::mat4 &matrix);
    void SetTextureGuid(const std::string &name, const std::string &textureGuid);

    /// Validate a Texture asset GUID or builtin white/black/normal token.
    /// Empty input explicitly clears the property; paths and missing assets fail.
    static std::string RequireTextureGuid(const std::string &textureGuid);
    void ClearTexture(const std::string &name);
    bool RemoveProperty(const std::string &name);

    [[nodiscard]] bool HasProperty(const std::string &name) const;
    [[nodiscard]] const MaterialProperty *GetProperty(const std::string &name) const;
    [[nodiscard]] const std::unordered_map<std::string, MaterialProperty> &GetAllProperties() const
    {
        return m_properties;
    }

    // ========================================================================
    // Pipeline State
    // ========================================================================

    [[nodiscard]] bool IsPipelineDirty() const
    {
        return m_pipelineDirty;
    }
    void ClearPipelineDirty()
    {
        m_pipelineDirty = false;
    }
    void MarkPipelineDirty()
    {
        m_pipelineDirty = true;
    }

    /// @brief Get a unique hash for this material's pipeline configuration
    [[nodiscard]] size_t GetPipelineHash() const;

    // ========================================================================
    // ShaderProgram integration (reflection-based UBO layout)
    // ========================================================================

    /// @brief Get unique shader ID for pipeline caching.
    [[nodiscard]] std::string GetShaderId() const
    {
        return m_vertexShader.StableKey() + "|" + m_fragmentShader.StableKey();
    }

    /// @brief Get a unique key for this material (for pipeline/render-data caching)
    [[nodiscard]] std::string GetMaterialKey() const
    {
        if (!m_guid.empty())
            return m_guid;
        if (!m_filePath.empty())
            return m_filePath;
        return "runtime-material:" + std::to_string(m_runtimeId);
    }

    // ========================================================================
    // Multi-pass pipeline storage
    //
    // Each material can hold independent pipeline data per compile target
    // (Forward, GBuffer, Shadow).  This replaces the old single-pipeline
    // + bolt-on shadow pipeline design.
    // ========================================================================

    /// Per-pass Vulkan pipeline data.
    struct PassPipeline
    {
        VkPipeline pipeline = VK_NULL_HANDLE;
        VkPipelineLayout layout = VK_NULL_HANDLE;
        VkDescriptorSet descriptorSet = VK_NULL_HANDLE;
        ShaderProgram *shaderProgram = nullptr;
    };

    /// Access per-pass pipeline data by compile target.
    void SetPassPipeline(ShaderCompileTarget target, VkPipeline pipeline)
    {
        PassPipeline_(target).pipeline = pipeline;
    }
    [[nodiscard]] VkPipeline GetPassPipeline(ShaderCompileTarget target) const
    {
        return PassPipeline_(target).pipeline;
    }

    void SetPassPipelineLayout(ShaderCompileTarget target, VkPipelineLayout layout)
    {
        PassPipeline_(target).layout = layout;
    }
    [[nodiscard]] VkPipelineLayout GetPassPipelineLayout(ShaderCompileTarget target) const
    {
        return PassPipeline_(target).layout;
    }

    void SetPassDescriptorSet(ShaderCompileTarget target, VkDescriptorSet set)
    {
        PassPipeline_(target).descriptorSet = set;
    }
    [[nodiscard]] VkDescriptorSet GetPassDescriptorSet(ShaderCompileTarget target) const
    {
        return PassPipeline_(target).descriptorSet;
    }

    void SetPassShaderProgram(ShaderCompileTarget target, ShaderProgram *program)
    {
        PassPipeline_(target).shaderProgram = program;
    }
    [[nodiscard]] ShaderProgram *GetPassShaderProgram(ShaderCompileTarget target) const
    {
        return PassPipeline_(target).shaderProgram;
    }

    /// Reset all pipeline data for a specific target.
    void ClearPassPipeline(ShaderCompileTarget target)
    {
        PassPipeline_(target) = PassPipeline{};
    }

    /// Reset all pass pipelines.
    void ClearAllPassPipelines()
    {
        for (int i = 0; i < static_cast<int>(ShaderCompileTarget::Count); ++i)
            m_passPipelines[i] = PassPipeline{};
    }

    /// Check if a specific pass variant has a valid pipeline.
    [[nodiscard]] bool HasPassPipeline(ShaderCompileTarget target) const
    {
        return PassPipeline_(target).pipeline != VK_NULL_HANDLE;
    }

    // ========================================================================
    // Serialization
    // ========================================================================

    [[nodiscard]] std::string Serialize() const;
    bool Deserialize(const std::string &jsonStr);
    [[nodiscard]] nlohmann::json SerializeDocument() const;
    bool DeserializeDocument(const nlohmann::json &document);

    /// @brief Create a default lit opaque material (engine built-in)
    static std::shared_ptr<InxMaterial> CreateDefaultLit();

    /// @brief Create a default unlit opaque material
    static std::shared_ptr<InxMaterial> CreateDefaultUnlit();

    /// @brief Create the default alpha-blended billboard particle material.
    static std::shared_ptr<InxMaterial> CreateParticleBillboardMaterial();

    /// @brief Create the default GPU ParticleGraph sprite material.
    static std::shared_ptr<InxMaterial> CreateParticleSpriteMaterial();

    /// @brief Create the read-only built-in six-way lit smoke particle material.
    static std::shared_ptr<InxMaterial> CreateParticleSixWaySmokeMaterial();

    /// @brief Create a gizmo material (uses gizmo shader, unlit, no depth write)
    static std::shared_ptr<InxMaterial> CreateGizmoMaterial();

    /// @brief Create a grid material (distance-fading alpha-blended grid)
    static std::shared_ptr<InxMaterial> CreateGridMaterial();

    /// @brief Create the editor tools material (translate/rotate/scale handles, no depth test)
    static std::shared_ptr<InxMaterial> CreateEditorToolsMaterial();

    /// @brief Create the component gizmos material (script-side, depth-tested, queue 30000)
    static std::shared_ptr<InxMaterial> CreateComponentGizmosMaterial();

    /// @brief Create the component gizmo icon material (TRIANGLE_LIST billboards, queue 31000)
    static std::shared_ptr<InxMaterial> CreateComponentGizmoIconMaterial();

    /// @brief Create the built-in textured camera icon billboard material.
    static std::shared_ptr<InxMaterial> CreateComponentGizmoCameraIconMaterial();

    /// @brief Create the built-in textured light icon billboard material.
    static std::shared_ptr<InxMaterial> CreateComponentGizmoLightIconMaterial();

    /// @brief Create the built-in textured particle-system icon billboard material.
    static std::shared_ptr<InxMaterial> CreateComponentGizmoParticleIconMaterial();

    /// @brief Create a procedural skybox material (gradient sky + sun)
    static std::shared_ptr<InxMaterial> CreateSkyboxProceduralMaterial();

    /// @brief Create the error material (purple-black checkerboard for shader mismatch)
    static std::shared_ptr<InxMaterial> CreateErrorMaterial();

    // ========================================================================
    // Clone (Unity-style Object.Instantiate for materials)
    // ========================================================================

    /// @brief Create a deep copy of this material (Unity: Object.Instantiate).
    /// Copies all properties, shader names, and render state.
    /// GPU-transient state (pipelines, UBO) is NOT copied — lazily recreated.
    /// The clone has no GUID and no file path (runtime-only instance).
    [[nodiscard]] std::shared_ptr<InxMaterial> Clone() const;
    [[nodiscard]] size_t GetRuntimeMemoryBytes() const noexcept;

    void SetGuid(const std::string &guid)
    {
        m_guid = guid;
    }

  private:
    static uint64_t AllocateRuntimeId() noexcept;
    bool ApplyDocument(const nlohmann::json &document);
    void SetPropertyValue(const std::string &name, MaterialPropertyType type, MaterialPropertyValue value);

    friend class MaterialLoader;

    std::string m_name;
    uint64_t m_runtimeId = AllocateRuntimeId();
    std::string m_guid;
    std::string m_filePath; // File path for saving
    bool m_builtin = false; // Built-in materials cannot have shader changed

    // Shader identity — separate vert/frag names allow different combinations.
    ShaderAssetReference m_vertexShader;
    ShaderAssetReference m_fragmentShader;

    // Pass tag for draw call filtering (set from @pass_tag shader annotation)
    std::string m_passTag;

    // Render state
    RenderState m_renderState;

    // Override bitmask: tracks which RenderState fields were set by the user
    // via the Material Inspector (survives shader annotation reapplication).
    uint32_t m_renderStateOverrides = 0;

    // Material properties
    std::unordered_map<std::string, MaterialProperty> m_properties;
    std::vector<std::string> m_shaderPropertyOrder;

    // Multi-pass pipeline storage
    // Indexed by ShaderCompileTarget: 0=Forward, 1=GBuffer, 2=Shadow
    PassPipeline m_passPipelines[static_cast<int>(ShaderCompileTarget::Count)];

    /// Internal accessor (mutable).
    PassPipeline &PassPipeline_(ShaderCompileTarget target = ShaderCompileTarget::Forward)
    {
        return m_passPipelines[static_cast<int>(target)];
    }
    /// Internal accessor (const).
    const PassPipeline &PassPipeline_(ShaderCompileTarget target = ShaderCompileTarget::Forward) const
    {
        return m_passPipelines[static_cast<int>(target)];
    }

    // Per-material UBO (Unity-style: each material has its own buffer)
    VkBuffer m_uboBuffer = VK_NULL_HANDLE;
    VmaAllocator m_uboAllocator = VK_NULL_HANDLE;
    VmaAllocation m_uboAllocation = VK_NULL_HANDLE;
    void *m_uboMappedData = nullptr;

    // Dirty flag for pipeline recreation
    bool m_pipelineDirty = true;

    // Dirty flag for properties (UBO needs update)
    bool m_propertiesDirty = true;

    // Monotonic version counter — bumped on every property/state change.
    // Python Inspector can poll this instead of full serialize() each frame.
    uint64_t m_version = 0;

    // True when the backing .mat file has been deleted from disk.
    // All holders should release or ignore a deleted material.
    bool m_isDeleted = false;

  public:
    // ========================================================================
    // Per-Material UBO (Unity-style)
    // ========================================================================

    void SetUBOBuffer(VmaAllocator allocator, VkBuffer buffer, VmaAllocation allocation, void *mappedData)
    {
        m_uboAllocator = allocator;
        m_uboBuffer = buffer;
        m_uboAllocation = allocation;
        m_uboMappedData = mappedData;
    }

    /// @brief Cleanup UBO resources (call before material destruction)
    void CleanupUBO(VkDevice device)
    {
        if (m_uboBuffer != VK_NULL_HANDLE && m_uboAllocator != VK_NULL_HANDLE) {
            m_uboMappedData = nullptr;
            vmaDestroyBuffer(m_uboAllocator, m_uboBuffer, m_uboAllocation);
            m_uboBuffer = VK_NULL_HANDLE;
            m_uboAllocation = VK_NULL_HANDLE;
        }
    }

    [[nodiscard]] DetachedUBO DetachUBO() noexcept
    {
        DetachedUBO resource{m_uboAllocator, m_uboBuffer, m_uboAllocation};
        m_uboAllocator = VK_NULL_HANDLE;
        m_uboBuffer = VK_NULL_HANDLE;
        m_uboAllocation = VK_NULL_HANDLE;
        m_uboMappedData = nullptr;
        return resource;
    }

    [[nodiscard]] VkBuffer GetUBOBuffer() const
    {
        return m_uboBuffer;
    }
    [[nodiscard]] VmaAllocation GetUBOAllocation() const
    {
        return m_uboAllocation;
    }
    [[nodiscard]] void *GetUBOMappedData() const
    {
        return m_uboMappedData;
    }
    [[nodiscard]] bool HasUBO() const
    {
        return m_uboBuffer != VK_NULL_HANDLE;
    }

    // ========================================================================
    // Properties Dirty Flag (for UBO sync optimization)
    // ========================================================================

    [[nodiscard]] bool IsPropertiesDirty() const
    {
        return m_propertiesDirty;
    }
    void ClearPropertiesDirty()
    {
        m_propertiesDirty = false;
    }
    void MarkPropertiesDirty()
    {
        m_propertiesDirty = true;
    }

    /// Monotonic version — incremented on every property / render-state change.
    [[nodiscard]] uint64_t GetVersion() const
    {
        return m_version;
    }
};

} // namespace infernux
