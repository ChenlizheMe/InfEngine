cmake_minimum_required(VERSION 3.17)

# ------------------------------------------------------------------------------
# Build Configuration
# ------------------------------------------------------------------------------
if(NOT CMAKE_CONFIGURATION_TYPES)
    set(_saved_build_type "${CMAKE_BUILD_TYPE}")
    set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

set(_saved_shared "${BUILD_SHARED_LIBS}")
set(BUILD_SHARED_LIBS ON CACHE BOOL "" FORCE)

# ------------------------------------------------------------------------------
# Global Options
# ------------------------------------------------------------------------------
set(BUILD_TESTING OFF CACHE BOOL "" FORCE)

# assimp configuration
set(ASSIMP_BUILD_ASSIMP_TOOLS OFF CACHE BOOL "" FORCE)
set(ASSIMP_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(ASSIMP_NO_EXPORT ON CACHE BOOL "" FORCE)
# IFC importer triggers a spurious MSVC /MP parallel compilation error
# (C2065 "_Ty1" in <map> from IFCUtil.h) — disable it; IFC (building
# CAD) is not needed for game asset pipelines.
set(ASSIMP_BUILD_IFC_IMPORTER OFF CACHE BOOL "" FORCE)
if(APPLE OR UNIX)
    # Use system zlib on macOS/Linux — assimp's bundled old zlib has
    # compilation issues with modern Apple Clang system headers.
    set(ASSIMP_BUILD_ZLIB OFF CACHE BOOL "" FORCE)
else()
    set(ASSIMP_BUILD_ZLIB ON CACHE BOOL "" FORCE)
endif()
set(ASSIMP_INSTALL OFF CACHE BOOL "" FORCE)

# glslang configuration
set(ENABLE_GLSLANG_BINARIES OFF CACHE BOOL "" FORCE)
set(GLSLANG_ENABLE_INSTALL OFF CACHE BOOL "" FORCE)
set(ENABLE_OPT OFF CACHE BOOL "" FORCE)
set(ENABLE_HLSL OFF CACHE BOOL "" FORCE)

# GLM configuration
set(GLM_BUILD_LIBRARY OFF CACHE BOOL "" FORCE)
set(GLM_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(GLM_BUILD_INSTALL OFF CACHE BOOL "" FORCE)

# SDL configuration
set(SDL_SHARED ON CACHE BOOL "" FORCE)
set(SDL_STATIC OFF CACHE BOOL "" FORCE)
set(SDL_OPENGL OFF CACHE BOOL "" FORCE)
set(SDL_OPENGLES OFF CACHE BOOL "" FORCE)

# Jolt Physics configuration
#
# SIMD baseline: distribution builds must run on CPUs without AVX (e.g. the
# Pentium/Celeron parts that ship with Intel UHD 610 graphics). Jolt defaults
# AVX/AVX2/F16C/LZCNT/TZCNT/FMADD to ON, which makes Jolt.dll's static
# initializers execute AVX instructions and fail with WinError 1114 on such
# machines (GitHub issue #47). Keep SSE4.2 (universal on x86-64 since ~2010)
# and disable everything newer.
set(USE_SSE4_1 ON CACHE BOOL "" FORCE)
set(USE_SSE4_2 ON CACHE BOOL "" FORCE)
set(USE_AVX OFF CACHE BOOL "" FORCE)
set(USE_AVX2 OFF CACHE BOOL "" FORCE)
set(USE_AVX512 OFF CACHE BOOL "" FORCE)
set(USE_LZCNT OFF CACHE BOOL "" FORCE)
set(USE_TZCNT OFF CACHE BOOL "" FORCE)
set(USE_F16C OFF CACHE BOOL "" FORCE)
set(USE_FMADD OFF CACHE BOOL "" FORCE)
# Cross-platform determinism: compile Jolt with precise FP (/fp:precise,
# -ffp-contract=off) and its deterministic math paths so the same simulation
# inputs produce identical trajectories on Windows/Linux/Android/Web. Required
# for reproducible headless simulation and RL experiments. FMADD must stay OFF
# (it already is above). JPH_CROSS_PLATFORM_DETERMINISTIC is a PUBLIC define,
# so engine translation units that include Jolt headers stay consistent.
set(CROSS_PLATFORM_DETERMINISTIC ON CACHE BOOL "" FORCE)
set(USE_STATIC_MSVC_RUNTIME_LIBRARY OFF CACHE BOOL "" FORCE)   # pybind11 requires /MD (dynamic CRT)
set(OVERRIDE_CXX_FLAGS OFF CACHE BOOL "" FORCE)                # Don't override parent project's CXX flags
set(INTERPROCEDURAL_OPTIMIZATION OFF CACHE BOOL "" FORCE)      # Avoid LTO issues with pybind11 module
set(DOUBLE_PRECISION OFF CACHE BOOL "" FORCE)                  # Single precision (matches GLM default)
set(GENERATE_DEBUG_SYMBOLS ON CACHE BOOL "" FORCE)
set(ENABLE_ALL_WARNINGS OFF CACHE BOOL "" FORCE)               # Avoid Jolt /Wall /WX breaking our build
set(ENABLE_OBJECT_STREAM ON CACHE BOOL "" FORCE)
set(ENABLE_INSTALL OFF CACHE BOOL "" FORCE)                    # We don't need install targets
set(CPP_EXCEPTIONS_ENABLED OFF CACHE BOOL "" FORCE)
set(CPP_RTTI_ENABLED OFF CACHE BOOL "" FORCE)
set(DEBUG_RENDERER_IN_DEBUG_AND_RELEASE OFF CACHE BOOL "" FORCE)  # Not needed for physics-only usage
set(PROFILER_IN_DEBUG_AND_RELEASE OFF CACHE BOOL "" FORCE)        # Not needed
set(JPH_USE_DX12 OFF CACHE BOOL "" FORCE)                     # We don't use Jolt's GPU compute
set(JPH_USE_VK OFF CACHE BOOL "" FORCE)                       # We have our own Vulkan renderer  
if(APPLE)
    set(JPH_USE_MTL ON CACHE BOOL "" FORCE)                     # macOS Metal backend for Jolt
else()
    set(JPH_USE_MTL OFF CACHE BOOL "" FORCE)
endif()
set(JPH_USE_CPU_COMPUTE ON CACHE BOOL "" FORCE)               # CPU physics only

# ------------------------------------------------------------------------------
# External Libraries
# ------------------------------------------------------------------------------

# ------------------------------------------------------------------------------
# InxPack codec — the only codec available to exported Player containers.
# ------------------------------------------------------------------------------
# A source override keeps offline and air-gapped builds deterministic.  When it
# is not supplied, FetchContent uses a pinned upstream release.  There is no
# silent fallback to zlib/Deflate or to the removed Python LZMA container.
option(INFERNUX_PLAYER_PACK_FETCH_ZSTD
    "Fetch the pinned Zstandard source when INFERNUX_ZSTD_SOURCE_DIR is empty"
    ON
)
set(INFERNUX_ZSTD_SOURCE_DIR "" CACHE PATH
    "Pinned local Zstandard source tree for offline Player package builds"
)
set(ZSTD_BUILD_PROGRAMS OFF CACHE BOOL "" FORCE)
set(ZSTD_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(ZSTD_BUILD_CONTRIB OFF CACHE BOOL "" FORCE)
set(ZSTD_BUILD_SHARED OFF CACHE BOOL "" FORCE)
set(ZSTD_BUILD_STATIC ON CACHE BOOL "" FORCE)
set(ZSTD_MULTITHREAD_SUPPORT OFF CACHE BOOL "" FORCE)
set(ZSTD_LEGACY_SUPPORT OFF CACHE BOOL "" FORCE)
if(INFERNUX_ZSTD_SOURCE_DIR)
    if(NOT EXISTS "${INFERNUX_ZSTD_SOURCE_DIR}/build/cmake/CMakeLists.txt")
        message(FATAL_ERROR
            "INFERNUX_ZSTD_SOURCE_DIR must point to a Zstandard source tree containing build/cmake/CMakeLists.txt"
        )
    endif()
    add_subdirectory(
        "${INFERNUX_ZSTD_SOURCE_DIR}/build/cmake"
        "${CMAKE_BINARY_DIR}/_deps/infernux-zstd-local"
        EXCLUDE_FROM_ALL
    )
elseif(INFERNUX_PLAYER_PACK_FETCH_ZSTD)
    include(FetchContent)
    FetchContent_Declare(
        infernux_zstd
        GIT_REPOSITORY https://github.com/facebook/zstd.git
        # Full commit pin: v1.5.6 (do not replace this with a moving tag).
        GIT_TAG 35016bc1c0b9a2f7121b7ecc312100aad7d9f2ad
        SOURCE_SUBDIR build/cmake
    )
    FetchContent_MakeAvailable(infernux_zstd)
else()
    message(FATAL_ERROR
        "Player InxPack requires Zstandard. Set INFERNUX_ZSTD_SOURCE_DIR to a pinned local checkout "
        "or enable INFERNUX_PLAYER_PACK_FETCH_ZSTD; no Deflate/LZMA fallback exists."
    )
endif()
if(TARGET libzstd_static)
    set(INFERNUX_ZSTD_TARGET libzstd_static PARENT_SCOPE)
elseif(TARGET zstd::libzstd_static)
    set(INFERNUX_ZSTD_TARGET zstd::libzstd_static PARENT_SCOPE)
else()
    message(FATAL_ERROR
        "The pinned Zstandard source did not provide the required libzstd_static target"
    )
endif()

# SPIRV-Cross is linked into target runtimes for shader reflection. A desktop
# Vulkan SDK only contains host libraries, so cross builds must compile the
# pinned source for the target ABI instead of accidentally linking host code.
set(_infernux_spirv_cross_default OFF)
if(CMAKE_CROSSCOMPILING)
    set(_infernux_spirv_cross_default ON)
endif()
option(INFERNUX_BUILD_TARGET_SPIRV_CROSS
    "Build pinned SPIRV-Cross sources for the current target"
    ${_infernux_spirv_cross_default}
)
set(INFERNUX_SPIRV_CROSS_SOURCE_DIR "" CACHE PATH
    "Pinned local SPIRV-Cross source tree for offline cross builds"
)
if(INFERNUX_BUILD_TARGET_SPIRV_CROSS)
    set(SPIRV_CROSS_SHARED OFF CACHE BOOL "" FORCE)
    set(SPIRV_CROSS_STATIC ON CACHE BOOL "" FORCE)
    set(SPIRV_CROSS_CLI OFF CACHE BOOL "" FORCE)
    set(SPIRV_CROSS_ENABLE_TESTS OFF CACHE BOOL "" FORCE)
    set(SPIRV_CROSS_ENABLE_GLSL ON CACHE BOOL "" FORCE)
    set(SPIRV_CROSS_ENABLE_HLSL OFF CACHE BOOL "" FORCE)
    set(SPIRV_CROSS_ENABLE_MSL OFF CACHE BOOL "" FORCE)
    set(SPIRV_CROSS_ENABLE_CPP OFF CACHE BOOL "" FORCE)
    set(SPIRV_CROSS_ENABLE_REFLECT OFF CACHE BOOL "" FORCE)
    set(SPIRV_CROSS_ENABLE_C_API OFF CACHE BOOL "" FORCE)
    set(SPIRV_CROSS_ENABLE_UTIL OFF CACHE BOOL "" FORCE)
    set(SPIRV_CROSS_SKIP_INSTALL ON CACHE BOOL "" FORCE)
    set(SPIRV_CROSS_FORCE_PIC ON CACHE BOOL "" FORCE)
    if(INFERNUX_SPIRV_CROSS_SOURCE_DIR)
        if(NOT EXISTS "${INFERNUX_SPIRV_CROSS_SOURCE_DIR}/CMakeLists.txt")
            message(FATAL_ERROR
                "INFERNUX_SPIRV_CROSS_SOURCE_DIR must contain CMakeLists.txt")
        endif()
        add_subdirectory(
            "${INFERNUX_SPIRV_CROSS_SOURCE_DIR}"
            "${CMAKE_BINARY_DIR}/_deps/infernux-spirv-cross-local"
            EXCLUDE_FROM_ALL
        )
    else()
        include(FetchContent)
        FetchContent_Declare(
            infernux_spirv_cross
            GIT_REPOSITORY https://github.com/KhronosGroup/SPIRV-Cross.git
            GIT_TAG 9c3c8e2cefdd8194b193bb8ed2fdff4d5527e382
        )
        FetchContent_MakeAvailable(infernux_spirv_cross)
    endif()
    if(NOT TARGET spirv-cross-core OR NOT TARGET spirv-cross-glsl)
        message(FATAL_ERROR "Pinned SPIRV-Cross did not provide core and GLSL targets")
    endif()
    set(INFERNUX_SPIRV_CROSS_TARGETS
        spirv-cross-core
        spirv-cross-glsl
        PARENT_SCOPE
    )
endif()

# Cross-platform Vulkan entry points cannot be assumed to exist as direct
# loader exports at the selected deployment API level. Build a pinned Volk
# meta-loader so core and extension commands share one dispatch path.
option(INFERNUX_BUILD_TARGET_VOLK
    "Build the pinned Volk Vulkan meta-loader for the current target"
    ${_infernux_spirv_cross_default}
)
set(INFERNUX_VOLK_SOURCE_DIR "" CACHE PATH
    "Pinned local Volk source tree for offline cross builds"
)
if(INFERNUX_BUILD_TARGET_VOLK)
    set(VOLK_INSTALL OFF CACHE BOOL "" FORCE)
    if(INFERNUX_VOLK_SOURCE_DIR)
        if(NOT EXISTS "${INFERNUX_VOLK_SOURCE_DIR}/volk.h" OR
           NOT EXISTS "${INFERNUX_VOLK_SOURCE_DIR}/CMakeLists.txt")
            message(FATAL_ERROR
                "INFERNUX_VOLK_SOURCE_DIR must contain volk.h and CMakeLists.txt")
        endif()
        add_subdirectory(
            "${INFERNUX_VOLK_SOURCE_DIR}"
            "${CMAKE_BINARY_DIR}/_deps/infernux-volk-local"
            EXCLUDE_FROM_ALL
        )
    else()
        include(FetchContent)
        FetchContent_Declare(
            infernux_volk
            GIT_REPOSITORY https://github.com/zeux/volk.git
            GIT_TAG 2e19a77ce9b5bd8df0106f66b91032c0e5c776a1
        )
        FetchContent_MakeAvailable(infernux_volk)
    endif()
    if(NOT TARGET volk)
        message(FATAL_ERROR "Pinned Volk source did not provide the volk target")
    endif()
    if(ANDROID)
        # Android loads the engine as several DSOs. Linking Volk's static
        # archive into each one creates independent hidden dispatch tables
        # while its public initialization functions remain interposable. A
        # call can then initialize one DSO's table and dereference another
        # DSO's null vkGetInstanceProcAddr. Keep exactly one process-wide
        # dispatch table in a shared runtime library instead.
        get_target_property(_infernux_volk_source_dir volk SOURCE_DIR)
        add_library(InfernuxVulkanLoader SHARED
            "${_infernux_volk_source_dir}/volk.c"
        )
        target_link_libraries(InfernuxVulkanLoader PUBLIC volk_headers)
        target_compile_definitions(InfernuxVulkanLoader PUBLIC
            VOLK_DEFAULT_VISIBILITY=1
        )
        set_target_properties(InfernuxVulkanLoader PROPERTIES
            C_VISIBILITY_PRESET default
            VISIBILITY_INLINES_HIDDEN OFF
        )
        add_custom_command(TARGET InfernuxVulkanLoader POST_BUILD
            COMMAND ${CMAKE_COMMAND}
                "-DSOURCE=$<TARGET_FILE:InfernuxVulkanLoader>"
                "-DDESTINATION=${PYTHON_TARGET_DIR}/$<TARGET_FILE_NAME:InfernuxVulkanLoader>"
                -P "${CMAKE_SOURCE_DIR}/cmake/stage_build_file.cmake"
            COMMENT "Stage the Android Vulkan dispatch library"
            VERBATIM
        )
        set(INFERNUX_VULKAN_LOADER_TARGET InfernuxVulkanLoader)
        set(INFERNUX_VULKAN_LOADER_TARGET InfernuxVulkanLoader PARENT_SCOPE)
    else()
        set(INFERNUX_VULKAN_LOADER_TARGET volk)
        set(INFERNUX_VULKAN_LOADER_TARGET volk PARENT_SCOPE)
    endif()
endif()

# assimp
add_subdirectory(assimp)
add_library(assimp::assimp ALIAS assimp)

# glslang
# Keep glslang static even though most runtime dependencies are shared.
# Its C++ API exposes some inline/non-exported helpers that are fragile when
# consumed through a Windows DLL import library with MSVC.
set(_saved_shared_for_glslang "${BUILD_SHARED_LIBS}")
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
add_subdirectory(glslang)
set(BUILD_SHARED_LIBS "${_saved_shared_for_glslang}" CACHE BOOL "" FORCE)
add_library(glslang::glslang ALIAS glslang)

# glm (header-only library)
add_subdirectory(glm)
# GLM already provides glm::glm target, no need for alias

# stb (header-only)
add_library(stb INTERFACE)
target_include_directories(stb INTERFACE ${CMAKE_CURRENT_SOURCE_DIR}/stb)

# dr_libs (header-only declarations; implementations live in AudioDecoders.cpp)
add_library(dr_libs INTERFACE)
target_include_directories(dr_libs INTERFACE ${CMAKE_CURRENT_SOURCE_DIR}/dr_libs)

# SDL3
add_subdirectory(SDL)

# Jolt Physics (CMakeLists.txt is in Build/ subdirectory)
add_subdirectory(JoltPhysics/Build JoltPhysics)

# Vulkan
find_package(Vulkan REQUIRED)

# ------------------------------------------------------------------------------
# VulkanMemoryAllocator (VMA) — GPU memory management (git submodule)
# ------------------------------------------------------------------------------
set(VMA_BUILD_DOCUMENTATION OFF CACHE BOOL "" FORCE)
set(VMA_BUILD_SAMPLES OFF CACHE BOOL "" FORCE)
add_subdirectory(VulkanMemoryAllocator)

# ------------------------------------------------------------------------------
# ImGui
# ------------------------------------------------------------------------------
set(IMGUI_DIR ${CMAKE_CURRENT_SOURCE_DIR}/imgui)

set(IMGUI_CORE_SRCS
    ${IMGUI_DIR}/imgui.cpp
    ${IMGUI_DIR}/imgui_draw.cpp
    ${IMGUI_DIR}/imgui_tables.cpp
    ${IMGUI_DIR}/imgui_widgets.cpp
)

set(INFERNUX_IMGUI_GENERATED_DIR "${CMAKE_BINARY_DIR}/generated/imgui")
set(INFERNUX_IMGUI_VULKAN_BACKEND "${INFERNUX_IMGUI_GENERATED_DIR}/imgui_impl_vulkan_infernux.cpp")
set(INFERNUX_IMGUI_FRAGMENT_SOURCE
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/renderer/gui/backend/infernux_imgui_frag.frag")
set(INFERNUX_IMGUI_FRAGMENT_HEADER
    "${INFERNUX_IMGUI_GENERATED_DIR}/infernux_imgui_frag.u32")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
    "${IMGUI_DIR}/backends/imgui_impl_vulkan.cpp"
    "${CMAKE_SOURCE_DIR}/cmake/patch_imgui_vulkan_backend.py"
    "${INFERNUX_IMGUI_FRAGMENT_SOURCE}"
)
file(MAKE_DIRECTORY "${INFERNUX_IMGUI_GENERATED_DIR}")
execute_process(
    COMMAND "${Vulkan_GLSLANG_VALIDATOR_EXECUTABLE}"
        -V -x
        -o "${INFERNUX_IMGUI_FRAGMENT_HEADER}"
        "${INFERNUX_IMGUI_FRAGMENT_SOURCE}"
    RESULT_VARIABLE INFERNUX_IMGUI_SHADER_RESULT
    ERROR_VARIABLE INFERNUX_IMGUI_SHADER_ERROR
)
if(NOT INFERNUX_IMGUI_SHADER_RESULT EQUAL 0)
    message(FATAL_ERROR "Failed to compile the Infernux ImGui fragment shader: ${INFERNUX_IMGUI_SHADER_ERROR}")
endif()
execute_process(
    COMMAND "${Python3_EXECUTABLE}"
        "${CMAKE_SOURCE_DIR}/cmake/patch_imgui_vulkan_backend.py"
        --source "${IMGUI_DIR}/backends/imgui_impl_vulkan.cpp"
        --output "${INFERNUX_IMGUI_VULKAN_BACKEND}"
    RESULT_VARIABLE INFERNUX_IMGUI_PATCH_RESULT
    ERROR_VARIABLE INFERNUX_IMGUI_PATCH_ERROR
)
if(NOT INFERNUX_IMGUI_PATCH_RESULT EQUAL 0)
    message(FATAL_ERROR "Failed to prepare the Infernux ImGui Vulkan backend: ${INFERNUX_IMGUI_PATCH_ERROR}")
endif()

set(IMGUI_BACKEND_SRCS
    ${INFERNUX_IMGUI_VULKAN_BACKEND}
    ${IMGUI_DIR}/backends/imgui_impl_sdl3.cpp
)

set(IMGUI_ALL_SRCS
    ${IMGUI_CORE_SRCS}
    ${IMGUI_BACKEND_SRCS}
)

add_library(imgui STATIC ${IMGUI_ALL_SRCS})
target_include_directories(imgui PUBLIC
    ${IMGUI_DIR}
    ${IMGUI_DIR}/backends
)
target_include_directories(imgui PRIVATE
    ${INFERNUX_IMGUI_GENERATED_DIR}
    ${CMAKE_SOURCE_DIR}/cpp/infernux/function/renderer/gui/backend
)

set(_infernux_external_vulkan_loader Vulkan::Vulkan)
if(INFERNUX_VULKAN_LOADER_TARGET)
    set(_infernux_external_vulkan_loader ${INFERNUX_VULKAN_LOADER_TARGET})
endif()
target_link_libraries(imgui PUBLIC
    ${_infernux_external_vulkan_loader}
    SDL3::SDL3
)
if(INFERNUX_VULKAN_LOADER_TARGET)
    target_compile_definitions(imgui PUBLIC
        IMGUI_IMPL_VULKAN_USE_VOLK=1
        INFERNUX_USE_VOLK=1
    )
endif()

# ------------------------------------------------------------------------------
# Restore Build Type
# ------------------------------------------------------------------------------
if(NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE "${_saved_build_type}" CACHE STRING "" FORCE)
endif()

set(BUILD_SHARED_LIBS "${_saved_shared}" CACHE BOOL "" FORCE)

# ------------------------------------------------------------------------------
# Export External Libraries
# ------------------------------------------------------------------------------
set(EXTERNAL_LIBS
    assimp::assimp
    glslang::glslang
    SDL3::SDL3
    glm::glm
    imgui
    stb
    Vulkan::Vulkan
    Jolt
    GPUOpen::VulkanMemoryAllocator
    PARENT_SCOPE
)
