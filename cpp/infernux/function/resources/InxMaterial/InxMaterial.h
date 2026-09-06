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
#if !defined(INFERNUX_DISABLE_VULKAN_MATERIAL_RUNTIME)
#include <vk_mem_alloc.h>
#include <vulkan/vulkan.h>
#endif

namespace infernux
{

// Forward declarations
class MeshRenderer;
class ShaderProgram;
struct MaterialUBOLayout;
struct ShaderProgramArtifact;

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
 * @brief Bitmask flags indicating which RenderState fields the material has
 *        explicitly authored (Inspector edit, Python setter, or a full
 *        SetRenderState() call).
 *
 * Shader annotations only provide *defaults*: when ApplyShaderRenderMeta()
 * is called, only fields whose corresponding override bit is NOT set will be
 * updated. Authored fields always win. Switching the material to a different
 * shader clears all bits so the new shader's annotation defaults become the
 * baseline, after which explicit material edits take over again.
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

/// Every authorable RenderState override bit. Used by SetRenderState() to
/// claim full authorship of the render state in one call.
inline constexpr uint32_t kAllRenderStateOverrides =
    static_cast<uint32_t>(RenderStateOverride::CullMode) | static_cast<uint32_t>(RenderStateOverride::DepthWrite) |
    static_cast<uint32_t>(RenderStateOverride::DepthTest) | static_cast<uint32_t>(RenderStateOverride::DepthCompareOp) |
    static_cast<uint32_t>(RenderStateOverride::BlendEnable) | static_cast<uint32_t>(RenderStateOverride::BlendMode) |
    static_cast<uint32_t>(RenderStateOverride::RenderQueue) | static_cast<uint32_t>(RenderStateOverride::SurfaceType) |
    static_cast<uint32_t>(RenderStateOverride::AlphaClip);

// Material documents are shared by Vulkan, WebGPU, and future renderer
// backends. Keep their persisted values independent from backend headers while
// retaining the numeric schema already used by .mat files and Python.
enum class MaterialCullMode : uint32_t
{
    None = 0,
    Front = 1,
    Back = 2,
    FrontAndBack = 3,
};

enum class MaterialFrontFace : uint32_t
{
    CounterClockwise = 0,
    Clockwise = 1,
};

enum class MaterialPolygonMode : uint32_t
{
    Fill = 0,
    Line = 1,
    Point = 2,
};

enum class MaterialPrimitiveTopology : uint32_t
{
    PointList = 0,
    LineList = 1,
    LineStrip = 2,
    TriangleList = 3,
    TriangleStrip = 4,
};

enum class MaterialCompareOp : uint32_t
{
    Never = 0,
    Less = 1,
    Equal = 2,
    LessOrEqual = 3,
    Greater = 4,
    NotEqual = 5,
    GreaterOrEqual = 6,
    Always = 7,
};

enum class MaterialStencilOp : uint32_t
{
    Keep = 0,
    Zero = 1,
    Replace = 2,
    IncrementAndClamp = 3,
    DecrementAndClamp = 4,
    Invert = 5,
    IncrementAndWrap = 6,
    DecrementAndWrap = 7,
};

struct MaterialStencilOpState
{
    MaterialStencilOp failOp = MaterialStencilOp::Keep;
    MaterialStencilOp passOp = MaterialStencilOp::Keep;
    MaterialStencilOp depthFailOp = MaterialStencilOp::Keep;
    MaterialCompareOp compareOp = MaterialCompareOp::Never;
    uint32_t compareMask = 0;
    uint32_t writeMask = 0;
    uint32_t reference = 0;

    [[nodiscard]] bool operator==(const MaterialStencilOpState &other) const noexcept
    {
        return failOp == other.failOp && passOp == other.passOp && depthFailOp == other.depthFailOp &&
               compareOp == other.compareOp && compareMask == other.compareMask && writeMask == other.writeMask &&
               reference == other.reference;
    }

    [[nodiscard]] bool operator!=(const MaterialStencilOpState &other) const noexcept
    {
        return !(*this == other);
    }
};

enum class MaterialBlendFactor : uint32_t
{
    Zero = 0,
    One = 1,
    SourceColor = 2,
    OneMinusSourceColor = 3,
    DestinationColor = 4,
    OneMinusDestinationColor = 5,
    SourceAlpha = 6,
    OneMinusSourceAlpha = 7,
    DestinationAlpha = 8,
    OneMinusDestinationAlpha = 9,
    ConstantColor = 10,
    OneMinusConstantColor = 11,
    ConstantAlpha = 12,
    OneMinusConstantAlpha = 13,
    SourceAlphaSaturate = 14,
};

enum class MaterialBlendOp : uint32_t
{
    Add = 0,
    Subtract = 1,
    ReverseSubtract = 2,
    Minimum = 3,
    Maximum = 4,
};

/**
 * @brief Render state configuration for materials
 *
 * This defines how the GPU should render geometry with this material.
 */
struct RenderState
{
    // Rasterization
    MaterialCullMode cullMode = MaterialCullMode::Back;
    MaterialFrontFace frontFace = MaterialFrontFace::Clockwise;
    MaterialPolygonMode polygonMode = MaterialPolygonMode::Fill;
    float lineWidth = 1.0f;

    // Depth bias (polygon offset) — pushes fragments in depth to avoid z-fighting
    bool depthBiasEnable = false;
    float depthBiasConstantFactor = 0.0f;
    float depthBiasSlopeFactor = 0.0f;
    float depthBiasClamp = 0.0f;

    // Primitive topology (default: triangle list)
    MaterialPrimitiveTopology topology = MaterialPrimitiveTopology::TriangleList;

    // Depth/Stencil
    bool depthTestEnable = true;
    bool depthWriteEnable = true;
    MaterialCompareOp depthCompareOp = MaterialCompareOp::Less;
    bool stencilTestEnable = false;
    MaterialStencilOpState stencilFront{}; // front-face stencil operations
    MaterialStencilOpState stencilBack{};  // back-face stencil operations

    // Blending
    bool blendEnable = false;
    MaterialBlendFactor srcColorBlendFactor = MaterialBlendFactor::SourceAlpha;
    MaterialBlendFactor dstColorBlendFactor = MaterialBlendFactor::OneMinusSourceAlpha;
    MaterialBlendOp colorBlendOp = MaterialBlendOp::Add;
    MaterialBlendFactor srcAlphaBlendFactor = MaterialBlendFactor::Zero;
    MaterialBlendFactor dstAlphaBlendFactor = MaterialBlendFactor::One;
    MaterialBlendOp alphaBlendOp = MaterialBlendOp::Add;

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
#if !defined(INFERNUX_DISABLE_VULKAN_MATERIAL_RUNTIME)
    struct DetachedUBO
    {
        VmaAllocator allocator = VK_NULL_HANDLE;
        VkBuffer buffer = VK_NULL_HANDLE;
        VmaAllocation allocation = VK_NULL_HANDLE;
    };
#endif
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

    /// Switching to another shader hands render-state authorship back to that
    /// shader's defaults. Declared before the inline shader setters so every
    /// supported compiler resolves the call consistently.
    void ResetRenderStateAuthorship();

    /// @brief Set both vertex and fragment shader to the same name (convenience).
    void SetShader(const std::string &shaderName)
    {
        const ShaderAssetReference reference{"", shaderName, ""};
        if (m_vertexShader == reference && m_fragmentShader == reference)
            return;
        const bool switched =
            !ReferencesSameShader(m_vertexShader, reference) || !ReferencesSameShader(m_fragmentShader, reference);
        m_vertexShader = reference;
        m_fragmentShader = reference;
        if (switched)
            ResetRenderStateAuthorship();
        m_pipelineDirty = true;
        ++m_version;
    }

    /// @brief Set vertex shader name independently.
    void SetVertShader(const std::string &name)
    {
        const ShaderAssetReference reference{"", name, ""};
        if (m_vertexShader == reference)
            return;
        const bool switched = !ReferencesSameShader(m_vertexShader, reference);
        m_vertexShader = reference;
        if (switched)
            ResetRenderStateAuthorship();
        m_pipelineDirty = true;
        ++m_version;
    }

    /// @brief Set fragment shader name independently.
    void SetFragShader(const std::string &name)
    {
        const ShaderAssetReference reference{"", name, ""};
        if (m_fragmentShader == reference)
            return;
        const bool switched = !ReferencesSameShader(m_fragmentShader, reference);
        m_fragmentShader = reference;
        if (switched)
            ResetRenderStateAuthorship();
        m_pipelineDirty = true;
        ++m_version;
    }

    void SetVertShaderReference(ShaderAssetReference reference)
    {
        if (m_vertexShader == reference)
            return;
        const bool switched = !ReferencesSameShader(m_vertexShader, reference);
        m_vertexShader = std::move(reference);
        if (switched)
            ResetRenderStateAuthorship();
        m_pipelineDirty = true;
        ++m_version;
    }

    void SetFragShaderReference(ShaderAssetReference reference)
    {
        if (m_fragmentShader == reference)
            return;
        const bool switched = !ReferencesSameShader(m_fragmentShader, reference);
        m_fragmentShader = std::move(reference);
        if (switched)
            ResetRenderStateAuthorship();
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
    /// @brief Explicitly author the full render state. Every field becomes an
    /// override, so shader annotation defaults can never replace it — the
    /// shader only supplies defaults for fields the material has not authored
    /// (at creation, or again after a shader switch clears authorship).
    void SetRenderState(const RenderState &state)
    {
        if (m_renderState == state && (m_renderStateOverrides & kAllRenderStateOverrides) == kAllRenderStateOverrides)
            return;
        m_renderState = state;
        m_renderStateOverrides |= kAllRenderStateOverrides;
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
    /// Render-state defaults imported from ShaderInfo.
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

    /// Fill properties omitted by a sparse material document from the linked
    /// shader contract. Existing authored values remain authoritative.
    bool SynchronizeShaderPropertyDefaults(const ShaderProgramArtifact &artifact);

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

    /// Per-pass shader publication plus backend-owned Vulkan state when the
    /// Vulkan material runtime is compiled.
    struct PassPipeline
    {
#if !defined(INFERNUX_DISABLE_VULKAN_MATERIAL_RUNTIME)
        VkPipeline pipeline = VK_NULL_HANDLE;
        VkPipelineLayout layout = VK_NULL_HANDLE;
        VkDescriptorSet descriptorSet = VK_NULL_HANDLE;
#endif
        std::shared_ptr<const ShaderProgram> shaderProgram;
    };

    /// Access per-pass pipeline data by compile target.
#if !defined(INFERNUX_DISABLE_VULKAN_MATERIAL_RUNTIME)
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
#endif

    void SetPassShaderProgram(ShaderCompileTarget target, std::shared_ptr<const ShaderProgram> program)
    {
        PassPipeline_(target).shaderProgram = std::move(program);
    }
    [[nodiscard]] const ShaderProgram *GetPassShaderProgram(ShaderCompileTarget target) const
    {
        return PassPipeline_(target).shaderProgram.get();
    }
    [[nodiscard]] const std::shared_ptr<const ShaderProgram> &
    GetPassShaderProgramPublication(ShaderCompileTarget target) const
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
#if !defined(INFERNUX_DISABLE_VULKAN_MATERIAL_RUNTIME)
        return PassPipeline_(target).pipeline != VK_NULL_HANDLE;
#else
        (void)target;
        return false;
#endif
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

    /// @brief Create the default double-sided transparent LineRenderer material.
    static std::shared_ptr<InxMaterial> CreateDefaultLineMaterial();

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

    // Pass tag for draw call filtering, initialized from ShaderInfo.
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

// Per-material Vulkan UBO. WebGPU and other backends own their material
// buffers through their backend runtime rather than storing foreign handles in
// the asset object.
#if !defined(INFERNUX_DISABLE_VULKAN_MATERIAL_RUNTIME)
    VkBuffer m_uboBuffer = VK_NULL_HANDLE;
    VmaAllocator m_uboAllocator = VK_NULL_HANDLE;
    VmaAllocation m_uboAllocation = VK_NULL_HANDLE;
#endif
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

#if !defined(INFERNUX_DISABLE_VULKAN_MATERIAL_RUNTIME)
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
#endif
    [[nodiscard]] void *GetUBOMappedData() const
    {
        return m_uboMappedData;
    }
    [[nodiscard]] bool HasUBO() const
    {
#if !defined(INFERNUX_DISABLE_VULKAN_MATERIAL_RUNTIME)
        return m_uboBuffer != VK_NULL_HANDLE;
#else
        return false;
#endif
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
