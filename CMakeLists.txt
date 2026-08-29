cmake_minimum_required(VERSION 3.22)
project(Infernux LANGUAGES C CXX)

if(MSVC)
    # Limit /MP globally, including third-party projects. CMake maps /MP to
    # MultiProcessorCompilation and the actual worker cap comes from this
    # Visual Studio global property.
    set(INFERNUX_MSVC_COMPILE_JOBS "4" CACHE STRING
        "Maximum MSVC translation units compiled concurrently per project")
    list(APPEND CMAKE_VS_GLOBALS "CL_MPCount=${INFERNUX_MSVC_COMPILE_JOBS}")
endif()

#------------------------------------------------------------------------------
# Linux: force -fPIC for all static libraries (fixes imgui linking error)
# ------------------------------------------------------------------------------
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# ------------------------------------------------------------------------------
# C++ Standard
# ------------------------------------------------------------------------------
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ------------------------------------------------------------------------------
# Paths and Directories
# ------------------------------------------------------------------------------
set(INFERNUX_PYTHON_SYNC_DIR "${CMAKE_SOURCE_DIR}/python/Infernux/lib" CACHE PATH
    "Directory that receives a development copy of the native Python runtime")
set(PYTHON_TARGET_DIR "${INFERNUX_PYTHON_SYNC_DIR}")

# ------------------------------------------------------------------------------
# Source Files
# ------------------------------------------------------------------------------
file(GLOB_RECURSE INFERNUX_RUNTIME_SOURCES CONFIGURE_DEPENDS cpp/*.cpp cpp/*.h)
list(APPEND INFERNUX_RUNTIME_SOURCES "${CMAKE_SOURCE_DIR}/external/stb/stb_vorbis.c")
# Runtime implementation and Python bindings are separate binary layers. This
# keeps pybind template churn out of the engine DLL and prevents a binding-only
# edit from relinking every renderer, scene, and resource object.
list(FILTER INFERNUX_RUNTIME_SOURCES EXCLUDE REGEX "tools/launcher/")
list(FILTER INFERNUX_RUNTIME_SOURCES EXCLUDE REGEX "tools/pybinding/")
# Tests are separate executables and must never be linked into a shipping binary.
list(FILTER INFERNUX_RUNTIME_SOURCES EXCLUDE REGEX "cpp/tests/")
file(GLOB_RECURSE BINDING_SOURCES CONFIGURE_DEPENDS cpp/infernux/tools/pybinding/*.cpp)
set(INFERNUX_BOOTSTRAP_BINDING_SOURCE
    "${CMAKE_SOURCE_DIR}/cpp/infernux/tools/pybinding/BindingInfernuxBootstrap.cpp"
)
list(REMOVE_ITEM BINDING_SOURCES ${INFERNUX_BOOTSTRAP_BINDING_SOURCE})

# Native DLL boundaries are source-owned rather than inferred from folders at
# link time. Foundation is dependency-free engine infrastructure. Particle
# Runtime is the camera-independent/RHI particle kernel and topology layer;
# Vulkan graph integration and material-facing renderers remain in Runtime.
file(GLOB_RECURSE INFERNUX_FOUNDATION_SOURCES CONFIGURE_DEPENDS
    cpp/infernux/core/*.cpp
    cpp/infernux/core/*.h
    cpp/infernux/platform/filesystem/*.cpp
    cpp/infernux/platform/filesystem/*.h
)
list(APPEND INFERNUX_FOUNDATION_SOURCES
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/resources/InxTexture/StbImage.cpp"
)
set(INFERNUX_PARTICLE_RUNTIME_SOURCES
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/renderer/particle/ParticleGpuBounds.cpp"
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/renderer/particle/ParticleGpuContinuationRuntime.cpp"
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/renderer/particle/ParticleGpuContactRuntime.cpp"
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/renderer/particle/ParticleGpuCollisionScene.cpp"
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/renderer/particle/ParticleGpuCuller.cpp"
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/renderer/particle/ParticleGpuDrawRegistry.cpp"
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/renderer/particle/ParticleGpuMigrator.cpp"
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/renderer/particle/ParticleGpuRibbonTopology.cpp"
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/renderer/particle/ParticleGpuRuntime.cpp"
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/renderer/particle/ParticleGpuSorter.cpp"
)
set(INFERNUX_SHADER_COMPILER_SOURCES
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/renderer/shader/ShaderCache.cpp"
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/renderer/shader/ShaderProgram.cpp"
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/renderer/shader/ShaderReflection.cpp"
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/resources/ShaderAsset/GlslStageInterfaceEmitter.cpp"
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/resources/ShaderAsset/ShaderInfoSchema.cpp"
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/resources/ShaderAsset/ShaderPassVariantPlanner.cpp"
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/resources/ShaderAsset/ShaderStageLinker.cpp"
)
file(GLOB_RECURSE INFERNUX_RENDER_CORE_SOURCES CONFIGURE_DEPENDS
    cpp/infernux/function/renderer/rhi/*.cpp
    cpp/infernux/function/renderer/rhi/*.h
)
set(INFERNUX_RENDERER_RUNTIME_SOURCES
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/scene/SceneRenderer.cpp"
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/scene/SceneRenderer.h"
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/renderer/Frustum.h"
    "${CMAKE_SOURCE_DIR}/cpp/infernux/function/renderer/RenderWorld.h"
)
file(GLOB_RECURSE INFERNUX_VULKAN_BACKEND_SOURCES CONFIGURE_DEPENDS
    cpp/infernux/function/renderer/vk/*.cpp
    cpp/infernux/function/renderer/vk/*.h
)
list(REMOVE_ITEM INFERNUX_RUNTIME_SOURCES
    ${INFERNUX_FOUNDATION_SOURCES}
    ${INFERNUX_PARTICLE_RUNTIME_SOURCES}
    ${INFERNUX_SHADER_COMPILER_SOURCES}
    ${INFERNUX_RENDER_CORE_SOURCES}
    ${INFERNUX_RENDERER_RUNTIME_SOURCES}
    ${INFERNUX_VULKAN_BACKEND_SOURCES}
)

# ------------------------------------------------------------------------------
# Header Include Directories
# ------------------------------------------------------------------------------
file(GLOB_RECURSE INFERNUX_HEADERS "${CMAKE_SOURCE_DIR}/cpp/*.h")
foreach(_hdr ${INFERNUX_HEADERS})
    get_filename_component(_hdr_dir "${_hdr}" DIRECTORY)
    list(APPEND INFERNUX_INCLUDE_DIRS "${_hdr_dir}")
endforeach()
list(REMOVE_DUPLICATES INFERNUX_INCLUDE_DIRS)

# ------------------------------------------------------------------------------
# Native Runtime And pybind11 Module Definition
# ------------------------------------------------------------------------------
set(PYBIND11_FINDPYTHON ON)
find_package(Python3 3.13 EXACT COMPONENTS Interpreter Development.Module Development.Embed REQUIRED)

# Auto-discover pip-installed pybind11 so find_package works out of the box
execute_process(
    COMMAND ${Python3_EXECUTABLE} -m pybind11 --cmakedir
    OUTPUT_VARIABLE _pybind11_cmakedir
    OUTPUT_STRIP_TRAILING_WHITESPACE
    ERROR_QUIET
    RESULT_VARIABLE _pybind11_result
)
if(_pybind11_result EQUAL 0 AND _pybind11_cmakedir)
    list(APPEND CMAKE_PREFIX_PATH "${_pybind11_cmakedir}")
endif()

find_package(pybind11 CONFIG REQUIRED)
find_package(Vulkan REQUIRED)

option(INFERNUX_BUILD_PLAYER_HOST
    "Build the native single-process PlayerHost bootstrap"
    OFF
)
option(INFERNUX_RUNTIME_STATIC
    "Link the private runtime composition layer statically into the Python module"
    ON
)
if(WIN32 AND INFERNUX_BUILD_PLAYER_HOST)
    include(InstallRequiredSystemLibraries)
endif()

# NO_EXTRAS keeps pybind11 from choosing its own LTO flags. Release IPO/LTO is
# enabled explicitly below where the target's export strategy supports it, so
# compile and link settings always agree.
add_library(InfernuxFoundation SHARED ${INFERNUX_FOUNDATION_SOURCES})
add_library(InfernuxParticleRuntime SHARED ${INFERNUX_PARTICLE_RUNTIME_SOURCES})
add_library(InfernuxShaderCompiler SHARED ${INFERNUX_SHADER_COMPILER_SOURCES})
add_library(InfernuxRenderCore SHARED ${INFERNUX_RENDER_CORE_SOURCES})
add_library(InfernuxRendererRuntime SHARED ${INFERNUX_RENDERER_RUNTIME_SOURCES})
add_library(InfernuxVulkanBackend SHARED ${INFERNUX_VULKAN_BACKEND_SOURCES})
# Shipping builds keep the composition layer as a private archive so the Python
# module can LTO/dead-strip the exact bound surface instead of shipping a
# 60k-symbol auto-export DLL. Native regression tests use the shared form to
# avoid loading a full private runtime image into every test process.
if(INFERNUX_RUNTIME_STATIC)
    add_library(InfernuxRuntime STATIC ${INFERNUX_RUNTIME_SOURCES})
else()
    add_library(InfernuxRuntime SHARED ${INFERNUX_RUNTIME_SOURCES})
endif()
pybind11_add_module(_Infernux MODULE NO_EXTRAS ${BINDING_SOURCES})
pybind11_add_module(_InfernuxBootstrap MODULE NO_EXTRAS ${INFERNUX_BOOTSTRAP_BINDING_SOURCE})

if(WIN32 AND INFERNUX_BUILD_PLAYER_HOST)
    add_executable(InfernuxPlayerHost WIN32
        cpp/infernux/tools/launcher/InfernuxPlayerLauncher.cpp
        cpp/infernux/tools/launcher/PlayerHost.cpp
        cpp/infernux/tools/launcher/PlayerHost.h
        cpp/infernux/tools/launcher/InfernuxPlayerLauncher.rc
    )
    set(INFERNUX_PLAYER_HOST_DIR
        "${CMAKE_SOURCE_DIR}/python/Infernux/resources/player_runtime"
    )
    set_target_properties(InfernuxPlayerHost PROPERTIES
        OUTPUT_NAME "InfernuxPlayerHost"
        MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
        RUNTIME_OUTPUT_DIRECTORY "${INFERNUX_PLAYER_HOST_DIR}"
        RUNTIME_OUTPUT_DIRECTORY_RELEASE "${INFERNUX_PLAYER_HOST_DIR}"
        RUNTIME_OUTPUT_DIRECTORY_RELWITHDEBINFO "${INFERNUX_PLAYER_HOST_DIR}"
        PDB_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/symbols"
        PDB_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}/symbols"
        PDB_OUTPUT_DIRECTORY_RELWITHDEBINFO "${CMAKE_BINARY_DIR}/symbols"
        COMPILE_PDB_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/symbols"
        COMPILE_PDB_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}/symbols"
        COMPILE_PDB_OUTPUT_DIRECTORY_RELWITHDEBINFO "${CMAKE_BINARY_DIR}/symbols"
    )
    if(MSVC)
        target_compile_options(InfernuxPlayerHost PRIVATE /utf-8 /O1)
        # Python 3.13's Windows headers inject python313.lib through
        # pragma comment(lib). PlayerHost resolves every PEP 587 symbol from
        # python313.dll at runtime, so an import-library dependency is invalid.
        target_link_options(InfernuxPlayerHost PRIVATE /NODEFAULTLIB:python313.lib)
    endif()
    target_include_directories(InfernuxPlayerHost PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
        ${Python3_INCLUDE_DIRS}
    )
    # PlayerHost is the only native code that runs before the Data archive is
    # extracted. Keep its bootstrap closure self-contained: linking any
    # Infernux shared library would reintroduce a loose DLL beside the game.
    target_sources(InfernuxPlayerHost PRIVATE
        cpp/infernux/platform/filesystem/InxPack.cpp
    )
    target_link_libraries(InfernuxPlayerHost PRIVATE shell32)
endif()

# Shared subsystem boundaries still predate explicit visibility annotations.
# The former monolithic composition DLL is deliberately absent from this list:
# it has no public ABI and is linked privately into the Python module with LTO.
set(INFERNUX_RUNTIME_DLL_TARGETS
    InfernuxFoundation
    InfernuxParticleRuntime
    InfernuxShaderCompiler
    InfernuxRenderCore
    InfernuxRendererRuntime
    InfernuxVulkanBackend
)
if(NOT INFERNUX_RUNTIME_STATIC)
    list(APPEND INFERNUX_RUNTIME_DLL_TARGETS InfernuxRuntime)
endif()
foreach(_infernux_dll ${INFERNUX_RUNTIME_DLL_TARGETS})
    set_target_properties(${_infernux_dll} PROPERTIES
        WINDOWS_EXPORT_ALL_SYMBOLS ON
        CXX_VISIBILITY_PRESET default
        VISIBILITY_INLINES_HIDDEN OFF
    )
endforeach()

set(INFERNUX_NATIVE_TARGETS
    InfernuxFoundation
    InfernuxParticleRuntime
    InfernuxShaderCompiler
    InfernuxRenderCore
    InfernuxRendererRuntime
    InfernuxVulkanBackend
    InfernuxRuntime
    _Infernux
    _InfernuxBootstrap
)
foreach(_infernux_target ${INFERNUX_NATIVE_TARGETS})
    target_include_directories(${_infernux_target} PRIVATE
        ${INFERNUX_INCLUDE_DIRS}
        ${CMAKE_SOURCE_DIR}/external
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
endforeach()

target_link_libraries(InfernuxParticleRuntime PUBLIC InfernuxFoundation)
target_link_libraries(InfernuxShaderCompiler PUBLIC InfernuxFoundation)
target_link_libraries(InfernuxRenderCore PUBLIC InfernuxFoundation)
target_link_libraries(InfernuxRendererRuntime PUBLIC InfernuxFoundation)
target_link_libraries(InfernuxVulkanBackend PUBLIC InfernuxRenderCore)
target_link_libraries(InfernuxRuntime PUBLIC
    InfernuxFoundation
    InfernuxParticleRuntime
    InfernuxShaderCompiler
    InfernuxRenderCore
    InfernuxRendererRuntime
    InfernuxVulkanBackend
)
target_link_libraries(InfernuxRuntime PRIVATE pybind11::embed)
target_link_libraries(_Infernux PRIVATE InfernuxRuntime)
target_link_libraries(_InfernuxBootstrap PRIVATE InfernuxFoundation)
if(WIN32)
    target_link_libraries(_InfernuxBootstrap PRIVATE user32)
endif()

# Set UTF-8 encoding for MSVC to avoid C4819 warnings
if(MSVC)
    # MSBuild already compiles independent projects concurrently. Unbounded
    # /MP inside every project multiplies that concurrency and intermittently
    # exhausts compiler, PDB, and file-system resources.
    foreach(_infernux_target ${INFERNUX_NATIVE_TARGETS})
        target_compile_options(${_infernux_target} PRIVATE
            /utf-8
            /MP
            /FS       # Serialize writes to the shared compiler PDB under parallel MSBuild.
            /bigobj   # pybind11 binding TUs exceed the default COFF section limit
        )
    endforeach()
elseif(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
    # IPO/LTO is configured through CMake below; keep only the optimization
    # level here so the compiler and linker receive one consistent policy.
    foreach(_infernux_target InfernuxRuntime _Infernux)
        target_compile_options(${_infernux_target} PRIVATE
            $<$<CONFIG:Release>:-O2>
        )
    endforeach()
endif()

option(INFERNUX_RELEASE_LTO "Enable IPO/LTO for native Release targets" ON)
if(INFERNUX_RELEASE_LTO)
    include(CheckIPOSupported)
    check_ipo_supported(RESULT _infernux_ipo_supported OUTPUT _infernux_ipo_error)
    if(_infernux_ipo_supported)
        set_property(TARGET _Infernux PROPERTY INTERPROCEDURAL_OPTIMIZATION_RELEASE TRUE)
        set_property(TARGET _InfernuxBootstrap PROPERTY INTERPROCEDURAL_OPTIMIZATION_RELEASE TRUE)
        set_property(TARGET InfernuxRuntime PROPERTY INTERPROCEDURAL_OPTIMIZATION_RELEASE TRUE)
        if(WIN32)
            # Auto-exported subsystem DLLs cannot consume MSVC /GL objects.
            # InfernuxRuntime is a private static composition archive and can
            # therefore participate in the _Infernux Release LTCG link.
            foreach(_infernux_dll ${INFERNUX_RUNTIME_DLL_TARGETS})
                set_property(TARGET ${_infernux_dll} PROPERTY INTERPROCEDURAL_OPTIMIZATION_RELEASE FALSE)
            endforeach()
        else()
            foreach(_infernux_dll ${INFERNUX_RUNTIME_DLL_TARGETS})
                set_property(TARGET ${_infernux_dll} PROPERTY INTERPROCEDURAL_OPTIMIZATION_RELEASE TRUE)
            endforeach()
        endif()
    else()
        message(FATAL_ERROR "Release IPO/LTO is required but unsupported: ${_infernux_ipo_error}")
    endif()
endif()

# ------------------------------------------------------------------------------
# SPIRV-Cross (from Vulkan SDK)
# ------------------------------------------------------------------------------
# SPIRV-Cross is included with the Vulkan SDK for shader reflection
if(DEFINED ENV{VULKAN_SDK})
    set(VULKAN_SDK_PATH $ENV{VULKAN_SDK})
    target_include_directories(InfernuxRuntime PRIVATE ${VULKAN_SDK_PATH}/Include)
    
    # Link SPIRV-Cross libraries for shader reflection
    if(WIN32)
        target_link_libraries(InfernuxShaderCompiler PRIVATE
            ${VULKAN_SDK_PATH}/Lib/spirv-cross-core.lib
            ${VULKAN_SDK_PATH}/Lib/spirv-cross-glsl.lib
            ${VULKAN_SDK_PATH}/Lib/spirv-cross-cpp.lib
            ${VULKAN_SDK_PATH}/Lib/spirv-cross-reflect.lib
        )
    else()
        # macOS / Linux: libraries live under lib/ with .a extension
        set(_spirv_cross_lib_dir "${VULKAN_SDK_PATH}/lib")
        foreach(_sc_lib spirv-cross-core spirv-cross-glsl spirv-cross-cpp spirv-cross-reflect)
            find_library(_found_${_sc_lib} NAMES ${_sc_lib} PATHS ${_spirv_cross_lib_dir} NO_DEFAULT_PATH)
            if(_found_${_sc_lib})
                target_link_libraries(InfernuxShaderCompiler PRIVATE ${_found_${_sc_lib}})
            else()
                message(WARNING "SPIRV-Cross library ${_sc_lib} not found in ${_spirv_cross_lib_dir}")
            endif()
        endforeach()
    endif()
else()
    # VULKAN_SDK not set — try system paths (e.g. Homebrew on macOS)
    set(_spirv_cross_found TRUE)
    foreach(_sc_lib spirv-cross-core spirv-cross-glsl spirv-cross-cpp spirv-cross-reflect)
        find_library(_sys_${_sc_lib} NAMES ${_sc_lib})
        if(_sys_${_sc_lib})
            target_link_libraries(InfernuxShaderCompiler PRIVATE ${_sys_${_sc_lib}})
        else()
            set(_spirv_cross_found FALSE)
        endif()
    endforeach()
    if(NOT _spirv_cross_found)
        message(WARNING "VULKAN_SDK not set and SPIRV-Cross libraries not found in system paths. Shader reflection will not be available.")
    endif()
endif()

# GLM: Use Vulkan-compatible [0,1] depth range instead of OpenGL [-1,1]
# GLM: Use left-handed coordinate system (Unity convention: X+=right, Y+=up, Z+=forward)
foreach(_infernux_target ${INFERNUX_NATIVE_TARGETS})
    target_compile_definitions(${_infernux_target} PRIVATE GLM_FORCE_DEPTH_ZERO_TO_ONE GLM_FORCE_LEFT_HANDED)
endforeach()

# Enable frame profiling instrumentation only in RelWithDebInfo builds.
# Terminal output is OFF by default; the dedicated debug-profile preset enables
# bounded profile dumps without changing the normal development build.
#
# File logging is independent from profiling:
# - Debug / RelWithDebInfo: truncate Logs/engine.log on startup and write through.
# - Release / MinSizeRel: keep only the last 100 log lines in memory and write them on exit.
option(INFERNUX_FRAME_PROFILE_TERMINAL "Emit bounded frame-profile reports to stderr" OFF)

# Vulkan validation is enabled for development configurations by default, but
# must be explicitly overridable without changing the optimization/debugging
# configuration. AUTO follows the configuration default; ON/OFF is an
# unambiguous command-line or preset override.
set(INFERNUX_ENABLE_VULKAN_VALIDATION "AUTO" CACHE STRING
    "Vulkan validation policy: AUTO (Debug/RelWithDebInfo on, Release off), ON, or OFF")
set_property(CACHE INFERNUX_ENABLE_VULKAN_VALIDATION PROPERTY STRINGS AUTO ON OFF)
string(TOUPPER "${INFERNUX_ENABLE_VULKAN_VALIDATION}" _infernux_vulkan_validation_policy)
if(NOT _infernux_vulkan_validation_policy MATCHES "^(AUTO|ON|OFF)$")
    message(FATAL_ERROR
        "INFERNUX_ENABLE_VULKAN_VALIDATION must be AUTO, ON, or OFF; got '${INFERNUX_ENABLE_VULKAN_VALIDATION}'")
endif()

if(_infernux_vulkan_validation_policy STREQUAL "ON")
    set(_infernux_vulkan_validation_non_release "1")
elseif(_infernux_vulkan_validation_policy STREQUAL "OFF")
    set(_infernux_vulkan_validation_non_release "0")
else()
    set(_infernux_vulkan_validation_non_release
        "$<IF:$<OR:$<CONFIG:Debug>,$<CONFIG:RelWithDebInfo>>,1,0>")
endif()

# Shipping configurations always disable Vulkan validation, even when the
# cache policy is explicitly ON. The policy only controls non-shipping configs.
set(_infernux_vulkan_validation_definition
    "INFERNUX_VULKAN_VALIDATION_LAYERS=$<IF:$<OR:$<CONFIG:Release>,$<CONFIG:MinSizeRel>>,0,${_infernux_vulkan_validation_non_release}>")

foreach(_infernux_target ${INFERNUX_NATIVE_TARGETS})
    target_compile_definitions(${_infernux_target} PRIVATE
        $<$<CONFIG:RelWithDebInfo>:INFERNUX_FRAME_PROFILE=1>
        $<$<CONFIG:Debug>:INFERNUX_FILE_LOGGING=1>
        $<$<CONFIG:RelWithDebInfo>:INFERNUX_FILE_LOGGING=1>
        $<$<CONFIG:Release>:INFERNUX_FILE_LOGGING=1>
        $<$<CONFIG:MinSizeRel>:INFERNUX_FILE_LOGGING=1>
        $<$<CONFIG:Release>:INFERNUX_DEFERRED_FILE_LOGGING=1>
        $<$<CONFIG:MinSizeRel>:INFERNUX_DEFERRED_FILE_LOGGING=1>
        $<$<OR:$<CONFIG:Release>,$<CONFIG:MinSizeRel>>:INFERNUX_COMPILE_OUT_DEBUG_LOGS=1>
        # Make diagnostics explicit for every configuration. Release and
        # MinSizeRel remain off regardless of the cache policy.
        ${_infernux_vulkan_validation_definition}
    )
endforeach()

if(INFERNUX_FRAME_PROFILE_TERMINAL)
    target_compile_definitions(InfernuxRuntime PRIVATE INFERNUX_FRAME_PROFILE_TERMINAL=1)
endif()

target_link_libraries(InfernuxRuntime PUBLIC
    assimp::assimp
    glslang::glslang
    SDL3::SDL3
    glm::glm
    imgui
    stb
    dr_libs
    Vulkan::Vulkan
    Jolt
    GPUOpen::VulkanMemoryAllocator
)
# ------------------------------------------------------------------------------
# External Dependencies
# ------------------------------------------------------------------------------
add_subdirectory(external)

target_link_libraries(InfernuxFoundation PUBLIC
    glm::glm
    stb
    Vulkan::Vulkan
)
target_link_libraries(InfernuxRenderCore PUBLIC glm::glm)
target_link_libraries(InfernuxRendererRuntime PUBLIC glm::glm)
target_link_libraries(InfernuxParticleRuntime PUBLIC glm::glm)
target_link_libraries(InfernuxShaderCompiler PUBLIC Vulkan::Vulkan)
target_link_libraries(InfernuxVulkanBackend PUBLIC
    SDL3::SDL3
    glm::glm
    Vulkan::Vulkan
    GPUOpen::VulkanMemoryAllocator
)

find_package(ZLIB QUIET)
if (TARGET zlibstatic)
    set(INFERNUX_ZLIB_TARGET zlibstatic)
    set(INFERNUX_ZLIB_INCLUDE_DIRS
        ${CMAKE_SOURCE_DIR}/external/assimp/contrib/zlib
        ${CMAKE_BINARY_DIR}/external/assimp/contrib/zlib
    )
elseif(TARGET ZLIB::ZLIB)
    set(INFERNUX_ZLIB_TARGET ZLIB::ZLIB)
elseif(TARGET ZLIB::zlib)
    set(INFERNUX_ZLIB_TARGET ZLIB::zlib)
else()
    message(FATAL_ERROR "Infernux requires a zlib target for document transaction journals")
endif()
target_link_libraries(InfernuxFoundation PRIVATE ${INFERNUX_ZLIB_TARGET})
if(INFERNUX_ZLIB_INCLUDE_DIRS)
    target_include_directories(InfernuxFoundation PRIVATE ${INFERNUX_ZLIB_INCLUDE_DIRS})
endif()
if(NOT INFERNUX_ZSTD_TARGET)
    message(FATAL_ERROR
        "InfernuxFoundation requires the native InxPack Zstandard target; "
        "set INFERNUX_ZSTD_SOURCE_DIR or enable INFERNUX_PLAYER_PACK_FETCH_ZSTD"
    )
endif()
target_link_libraries(InfernuxFoundation PRIVATE ${INFERNUX_ZSTD_TARGET})
if(TARGET InfernuxPlayerHost)
    target_link_libraries(InfernuxPlayerHost PRIVATE ${INFERNUX_ZSTD_TARGET})
endif()

# ------------------------------------------------------------------------------
# Native regression tests
# ------------------------------------------------------------------------------
include("${CMAKE_SOURCE_DIR}/cpp/tests/CMakeLists.txt")

# ------------------------------------------------------------------------------
# Native staging and install contract
# ------------------------------------------------------------------------------
include("${CMAKE_SOURCE_DIR}/cmake/InfernuxInstall.cmake")

# ------------------------------------------------------------------------------
# Infernux Hub standalone packaging targets
# ------------------------------------------------------------------------------
include("${CMAKE_SOURCE_DIR}/cmake/InfernuxHub.cmake")

# ------------------------------------------------------------------------------
# Developer maintenance targets
# ------------------------------------------------------------------------------
include("${CMAKE_SOURCE_DIR}/cmake/InfernuxDeveloperTools.cmake")

# ------------------------------------------------------------------------------
# Python and official-plugin package assembly
# ------------------------------------------------------------------------------
include("${CMAKE_SOURCE_DIR}/cmake/InfernuxPackaging.cmake")
