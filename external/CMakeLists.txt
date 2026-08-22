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
    set(ZSTD_BUILD_PROGRAMS OFF CACHE BOOL "" FORCE)
    set(ZSTD_BUILD_TESTS OFF CACHE BOOL "" FORCE)
    set(ZSTD_BUILD_CONTRIB OFF CACHE BOOL "" FORCE)
    set(ZSTD_BUILD_SHARED OFF CACHE BOOL "" FORCE)
    set(ZSTD_BUILD_STATIC ON CACHE BOOL "" FORCE)
    set(ZSTD_MULTITHREAD_SUPPORT OFF CACHE BOOL "" FORCE)
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

target_link_libraries(imgui PUBLIC
    Vulkan::Vulkan
    SDL3::SDL3
)

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
