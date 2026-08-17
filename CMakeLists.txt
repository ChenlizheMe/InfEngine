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
find_package(Python3 COMPONENTS Interpreter Development.Module Development.Embed REQUIRED)

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
add_library(InfernuxRuntime SHARED ${INFERNUX_RUNTIME_SOURCES})
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
        "${CMAKE_SOURCE_DIR}/python/Infernux/resources/player"
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
        # Python 3.12's Windows headers inject python312.lib through
        # pragma comment(lib). PlayerHost resolves every PEP 587 symbol from
        # python312.dll at runtime, so an import-library dependency is invalid.
        target_link_options(InfernuxPlayerHost PRIVATE /NODEFAULTLIB:python312.lib)
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

# Most of the existing runtime API predates explicit visibility annotations.
# CMake generates the Windows export table while Linux/macOS use their default
# symbol visibility. A later ABI-hardening pass can replace this with a narrow
# INFERNUX_API surface without collapsing the binary boundary again.
set(INFERNUX_RUNTIME_DLL_TARGETS
    InfernuxFoundation
    InfernuxParticleRuntime
    InfernuxShaderCompiler
    InfernuxRenderCore
    InfernuxRendererRuntime
    InfernuxVulkanBackend
    InfernuxRuntime
)
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
    # Keep the historical runtime-DLL linker policy until its public ABI uses
    # explicit INFERNUX_API exports. The pybind module does not need this
    # workaround and can use the standard optimized Release linker policy.
    target_link_options(InfernuxRuntime PRIVATE
        $<$<CONFIG:Release>:/OPT:NOREF>
        $<$<CONFIG:Release>:/OPT:NOICF>
    )
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
        if(WIN32)
            # WINDOWS_EXPORT_ALL_SYMBOLS cannot inspect MSVC /GL objects, so
            # enabling IPO here removes the DLL export table. Keep the runtime
            # stable until its public ABI has explicit export annotations.
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

if(TARGET zlibstatic)
    set(INFERNUX_ZLIB_TARGET zlibstatic)
    set(INFERNUX_ZLIB_INCLUDE_DIRS
        ${CMAKE_SOURCE_DIR}/external/assimp/contrib/zlib
        ${CMAKE_BINARY_DIR}/external/assimp/contrib/zlib
    )
elseif(TARGET ZLIB::zlib)
    set(INFERNUX_ZLIB_TARGET ZLIB::zlib)
elseif(TARGET ZLIB::ZLIB)
    set(INFERNUX_ZLIB_TARGET ZLIB::ZLIB)
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
option(INFERNUX_BUILD_TESTS "Build Infernux native regression tests" ON)
if(INFERNUX_BUILD_TESTS)
    enable_testing()
    find_package(Threads REQUIRED)

    add_executable(infernux_path_tests
        cpp/tests/PathTests.cpp
    )
    target_include_directories(infernux_path_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_path_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.path COMMAND infernux_path_tests)

    add_executable(infernux_inxpack_tests
        cpp/tests/InxPackTests.cpp
    )
    target_include_directories(infernux_inxpack_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_inxpack_tests PRIVATE InfernuxFoundation)
    if(MSVC)
        target_compile_options(infernux_inxpack_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.inxpack COMMAND infernux_inxpack_tests)

    if(WIN32)
        add_executable(infernux_player_host_tests
            cpp/tests/PlayerHostTests.cpp
            cpp/infernux/tools/launcher/PlayerHost.cpp
            cpp/infernux/tools/launcher/PlayerHost.h
        )
        target_include_directories(infernux_player_host_tests PRIVATE
            ${CMAKE_SOURCE_DIR}/cpp
            ${CMAKE_SOURCE_DIR}/cpp/infernux
            ${CMAKE_SOURCE_DIR}/external
            ${Python3_INCLUDE_DIRS}
        )
        target_sources(infernux_player_host_tests PRIVATE
            cpp/infernux/platform/filesystem/InxPack.cpp
        )
        target_link_libraries(infernux_player_host_tests PRIVATE ${INFERNUX_ZSTD_TARGET} shell32)
        set_target_properties(infernux_player_host_tests PROPERTIES
            MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
        )
        if(MSVC)
            target_compile_options(infernux_player_host_tests PRIVATE /utf-8)
            target_link_options(infernux_player_host_tests PRIVATE /NODEFAULTLIB:python312.lib)
        endif()
        add_test(NAME infernux.player_host COMMAND infernux_player_host_tests)
    endif()

    add_executable(infernux_editor_search_model_tests
        cpp/tests/EditorSearchModelTests.cpp
    )
    target_include_directories(infernux_editor_search_model_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_editor_search_model_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.editor_search_model COMMAND infernux_editor_search_model_tests)

    add_executable(infernux_editor_collection_model_tests
        cpp/tests/EditorCollectionModelTests.cpp
    )
    target_include_directories(infernux_editor_collection_model_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_editor_collection_model_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.editor_collection_model COMMAND infernux_editor_collection_model_tests)

    add_executable(infernux_job_system_tests
        cpp/tests/JobSystemTests.cpp
        cpp/infernux/core/log/InxLog.cpp
        cpp/infernux/core/threading/JobSystem.cpp
    )
    target_include_directories(infernux_job_system_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    target_link_libraries(infernux_job_system_tests PRIVATE Threads::Threads)
    if(MSVC)
        target_compile_options(infernux_job_system_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.job_system COMMAND infernux_job_system_tests)
    set_tests_properties(infernux.job_system PROPERTIES TIMEOUT 30)

    add_executable(infernux_jolt_job_system_adapter_tests
        cpp/tests/InfernuxJoltJobSystemAdapterTests.cpp
        cpp/infernux/function/scene/physics/InfernuxJoltJobSystemAdapter.cpp
        cpp/infernux/core/log/InxLog.cpp
        cpp/infernux/core/threading/JobSystem.cpp
    )
    target_include_directories(infernux_jolt_job_system_adapter_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_jolt_job_system_adapter_tests PRIVATE Jolt Threads::Threads)
    if(MSVC)
        target_compile_options(infernux_jolt_job_system_adapter_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.jolt_job_system_adapter COMMAND infernux_jolt_job_system_adapter_tests)
    set_tests_properties(infernux.jolt_job_system_adapter PROPERTIES TIMEOUT 30)

    add_executable(infernux_gpu_retirement_queue_tests
        cpp/tests/GpuRetirementQueueTests.cpp
    )
    target_include_directories(infernux_gpu_retirement_queue_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_gpu_retirement_queue_tests PRIVATE /utf-8)
    endif()
    target_link_libraries(infernux_gpu_retirement_queue_tests PRIVATE InfernuxRenderCore)
    add_test(NAME infernux.gpu_retirement_queue COMMAND infernux_gpu_retirement_queue_tests)

    add_executable(infernux_editor_gui_frame_scheduler_tests
        cpp/tests/EditorGuiFrameSchedulerTests.cpp
    )
    target_include_directories(infernux_editor_gui_frame_scheduler_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_editor_gui_frame_scheduler_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.editor_gui_frame_scheduler COMMAND infernux_editor_gui_frame_scheduler_tests)

    add_executable(infernux_log_release_policy_tests
        cpp/tests/InxLogReleasePolicyTests.cpp
        cpp/infernux/core/log/InxLog.cpp
    )
    target_include_directories(infernux_log_release_policy_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    target_compile_definitions(infernux_log_release_policy_tests PRIVATE
        INFERNUX_COMPILE_OUT_DEBUG_LOGS=1
    )
    if(MSVC)
        target_compile_options(infernux_log_release_policy_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.log_release_policy COMMAND infernux_log_release_policy_tests)

    add_executable(infernux_input_manager_tests
        cpp/tests/InputManagerTests.cpp
        cpp/infernux/platform/input/InputManager.cpp
        cpp/infernux/core/log/InxLog.cpp
    )
    target_include_directories(infernux_input_manager_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    target_link_libraries(infernux_input_manager_tests PRIVATE SDL3::SDL3)
    if(MSVC)
        target_compile_options(infernux_input_manager_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.input_manager COMMAND infernux_input_manager_tests)

    add_executable(infernux_atomic_file_tests
        cpp/tests/AtomicFileTests.cpp
        cpp/infernux/platform/filesystem/AtomicFile.cpp
    )
    target_include_directories(infernux_atomic_file_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_atomic_file_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.atomic_file COMMAND infernux_atomic_file_tests)

    add_executable(infernux_document_store_tests
        cpp/tests/DocumentStoreTests.cpp
        cpp/infernux/platform/filesystem/DocumentStore.cpp
        cpp/infernux/platform/filesystem/AtomicFile.cpp
    )
    target_include_directories(infernux_document_store_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_document_store_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.document_store COMMAND infernux_document_store_tests)

    add_executable(infernux_document_transaction_tests
        cpp/tests/DocumentTransactionTests.cpp
        cpp/infernux/platform/filesystem/DocumentTransaction.cpp
        cpp/infernux/platform/filesystem/DocumentStore.cpp
        cpp/infernux/platform/filesystem/AtomicFile.cpp
    )
    target_include_directories(infernux_document_transaction_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    target_link_libraries(infernux_document_transaction_tests PRIVATE Threads::Threads)
    target_link_libraries(infernux_document_transaction_tests PRIVATE ${INFERNUX_ZLIB_TARGET})
    if(INFERNUX_ZLIB_INCLUDE_DIRS)
        target_include_directories(infernux_document_transaction_tests PRIVATE ${INFERNUX_ZLIB_INCLUDE_DIRS})
    endif()
    if(MSVC)
        target_compile_options(infernux_document_transaction_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.document_transaction COMMAND infernux_document_transaction_tests)

    add_executable(infernux_component_data_store_tests
        cpp/tests/ComponentDataStoreTests.cpp
        cpp/infernux/function/scene/ComponentDataStore.cpp
    )
    target_include_directories(infernux_component_data_store_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_component_data_store_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_component_data_store_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.component_data_store COMMAND infernux_component_data_store_tests)

    add_executable(infernux_component_document_validation_tests
        cpp/tests/ComponentDocumentValidationTests.cpp
        cpp/infernux/function/scene/ComponentDocumentValidation.cpp
    )
    target_include_directories(infernux_component_document_validation_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    if(MSVC)
        target_compile_options(infernux_component_document_validation_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.component_document_validation COMMAND infernux_component_document_validation_tests)

    add_executable(infernux_render_identity_tests
        cpp/tests/RenderIdentityTests.cpp
    )
    target_include_directories(infernux_render_identity_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_render_identity_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.render_identity COMMAND infernux_render_identity_tests)

    add_executable(infernux_canonical_light_snapshot_tests
        cpp/tests/CanonicalLightSnapshotTests.cpp
        cpp/infernux/function/renderer/lighting/CanonicalLightGpuBuffer.cpp
        cpp/infernux/function/renderer/lighting/ForwardPlusLightGrid.cpp
    )
    target_include_directories(infernux_canonical_light_snapshot_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_canonical_light_snapshot_tests PRIVATE glm::glm)
    # Match the runtime GLM conventions so shadow math is tested under the
    # exact same handedness and clip-depth range the engine renders with.
    target_compile_definitions(infernux_canonical_light_snapshot_tests PRIVATE
        GLM_FORCE_DEPTH_ZERO_TO_ONE
        GLM_FORCE_LEFT_HANDED
    )
    if(MSVC)
        target_compile_options(infernux_canonical_light_snapshot_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.canonical_light_snapshot COMMAND infernux_canonical_light_snapshot_tests)

    add_executable(infernux_render_instance_history_tests
        cpp/tests/RenderInstanceHistoryTests.cpp
    )
    target_include_directories(infernux_render_instance_history_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_render_instance_history_tests PRIVATE glm::glm)
    if(MSVC)
        target_compile_options(infernux_render_instance_history_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.render_instance_history COMMAND infernux_render_instance_history_tests)

    add_executable(infernux_material_pass_pipeline_tests
        cpp/tests/MaterialPassPipelineTests.cpp
    )
    target_include_directories(infernux_material_pass_pipeline_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_material_pass_pipeline_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.material_pass_pipeline COMMAND infernux_material_pass_pipeline_tests)

    add_executable(infernux_render_graph_identity_tests
        cpp/tests/RenderGraphIdentityTests.cpp
    )
    target_include_directories(infernux_render_graph_identity_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_render_graph_identity_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.render_graph_identity COMMAND infernux_render_graph_identity_tests)

    add_executable(infernux_rhi_query_tests
        cpp/tests/RhiQueryTests.cpp
    )
    target_include_directories(infernux_rhi_query_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_rhi_query_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.rhi_query COMMAND infernux_rhi_query_tests)

    add_executable(infernux_rhi_capabilities_tests
        cpp/tests/RhiCapabilitiesTests.cpp
    )
    target_include_directories(infernux_rhi_capabilities_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_rhi_capabilities_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.rhi_capabilities COMMAND infernux_rhi_capabilities_tests)

    add_executable(infernux_rhi_resource_index_tests
        cpp/tests/RhiResourceIndexTests.cpp
    )
    target_include_directories(infernux_rhi_resource_index_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_rhi_resource_index_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.rhi_resource_index COMMAND infernux_rhi_resource_index_tests)

    add_executable(infernux_msaa_policy_tests
        cpp/tests/MsaaPolicyTests.cpp
    )
    target_include_directories(infernux_msaa_policy_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_msaa_policy_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.msaa_policy COMMAND infernux_msaa_policy_tests)

    add_executable(infernux_frustum_tests
        cpp/tests/FrustumTests.cpp
    )
    target_include_directories(infernux_frustum_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    target_link_libraries(infernux_frustum_tests PRIVATE glm::glm)
    target_compile_definitions(infernux_frustum_tests PRIVATE
        GLM_FORCE_DEPTH_ZERO_TO_ONE
        GLM_FORCE_LEFT_HANDED
    )
    if(MSVC)
        target_compile_options(infernux_frustum_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.frustum COMMAND infernux_frustum_tests)

    add_executable(infernux_rhi_contracts_tests
        cpp/tests/RhiContractsTests.cpp
    )
    target_include_directories(infernux_rhi_contracts_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    target_link_libraries(infernux_rhi_contracts_tests PRIVATE InfernuxRenderCore)
    if(MSVC)
        target_compile_options(infernux_rhi_contracts_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_rhi_contracts_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.rhi_contracts COMMAND infernux_rhi_contracts_tests)

    add_executable(infernux_vulkan_rhi_device_tests
        cpp/tests/VulkanRhiDeviceTests.cpp
        cpp/infernux/core/log/InxLog.cpp
        cpp/infernux/function/renderer/rhi/RhiDeviceIdentity.cpp
        cpp/infernux/function/renderer/vk/DescriptorBindTrace.cpp
        cpp/infernux/function/renderer/vk/VmaContext.cpp
        cpp/infernux/function/renderer/vk/VkDescriptorManager.cpp
        cpp/infernux/function/renderer/vk/VulkanBindlessTextureTable.cpp
        cpp/infernux/function/renderer/vk/VulkanRhiDevice.cpp
    )
    target_include_directories(infernux_vulkan_rhi_device_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    target_link_libraries(infernux_vulkan_rhi_device_tests PRIVATE
        Vulkan::Vulkan
        GPUOpen::VulkanMemoryAllocator
    )
    if(MSVC)
        target_compile_options(infernux_vulkan_rhi_device_tests PRIVATE /utf-8)
        target_link_libraries(infernux_vulkan_rhi_device_tests PRIVATE delayimp)
        target_link_options(infernux_vulkan_rhi_device_tests PRIVATE /DELAYLOAD:vulkan-1.dll)
    endif()
    add_test(NAME infernux.vulkan_rhi_device COMMAND infernux_vulkan_rhi_device_tests)
    set_tests_properties(infernux.vulkan_rhi_device PROPERTIES TIMEOUT 30)

    add_executable(infernux_vulkan_submission_executor_tests
        cpp/tests/VulkanSubmissionExecutorTests.cpp
    )
    target_include_directories(infernux_vulkan_submission_executor_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    target_link_libraries(infernux_vulkan_submission_executor_tests PRIVATE InfernuxVulkanBackend)
    if(MSVC)
        target_compile_options(infernux_vulkan_submission_executor_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_vulkan_submission_executor_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.vulkan_submission_executor COMMAND infernux_vulkan_submission_executor_tests)
    set_tests_properties(infernux.vulkan_submission_executor PROPERTIES
        LABELS "requires_vulkan_device"
        TIMEOUT 60
    )

    add_executable(infernux_scene_render_graph_validation_tests
        cpp/tests/SceneRenderGraphValidationTests.cpp
    )
    target_include_directories(infernux_scene_render_graph_validation_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    target_link_libraries(infernux_scene_render_graph_validation_tests PRIVATE InfernuxRuntime)
    if(MSVC)
        target_compile_options(infernux_scene_render_graph_validation_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.scene_render_graph_validation COMMAND infernux_scene_render_graph_validation_tests)

    add_executable(infernux_py_component_proxy_tests
        cpp/tests/PyComponentProxyTests.cpp
    )
    target_include_directories(infernux_py_component_proxy_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_py_component_proxy_tests PRIVATE
        InfernuxRuntime
        pybind11::embed
    )
    if(MSVC)
        target_compile_options(infernux_py_component_proxy_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.py_component_proxy COMMAND infernux_py_component_proxy_tests)

    add_executable(infernux_runtime_frame_barrier_tests
        cpp/tests/RuntimeFrameBarrierTests.cpp
    )
    target_include_directories(infernux_runtime_frame_barrier_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_runtime_frame_barrier_tests PRIVATE InfernuxRuntime)
    if(MSVC)
        target_compile_options(infernux_runtime_frame_barrier_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_runtime_frame_barrier_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.runtime_frame_barrier COMMAND infernux_runtime_frame_barrier_tests)

    add_executable(infernux_runtime_persistent_scene_tests
        cpp/tests/RuntimePersistentSceneTests.cpp
    )
    target_include_directories(infernux_runtime_persistent_scene_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_runtime_persistent_scene_tests PRIVATE InfernuxRuntime)
    if(MSVC)
        target_compile_options(infernux_runtime_persistent_scene_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_runtime_persistent_scene_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.runtime_persistent_scene COMMAND infernux_runtime_persistent_scene_tests)

    add_executable(infernux_particle_gpu_runtime_tests
        cpp/tests/ParticleGpuRuntimeTests.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuBillboardRenderer.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuSurfaceBinding.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuMeshRenderer.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuBounds.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuContinuationRuntime.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuContactRuntime.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuCollisionScene.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuDrawRegistry.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuMigrator.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuRibbonRenderer.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuRibbonTopology.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuRuntime.cpp
    )
    target_include_directories(infernux_particle_gpu_runtime_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_particle_gpu_runtime_tests PRIVATE InfernuxRuntime)
    if(MSVC)
        target_compile_options(infernux_particle_gpu_runtime_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_particle_gpu_runtime_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.particle_gpu_runtime COMMAND infernux_particle_gpu_runtime_tests)

    set(INFERNUX_COMPUTE_INDIRECT_SHADER_DIR "${CMAKE_CURRENT_BINARY_DIR}/tests/shaders")
    set(INFERNUX_COMPUTE_INDIRECT_SHADER_SOURCES
        "${CMAKE_SOURCE_DIR}/cpp/tests/shaders/RenderGraphComputeIndirect.comp"
        "${CMAKE_SOURCE_DIR}/cpp/tests/shaders/RenderGraphComputeIndirect.vert"
        "${CMAKE_SOURCE_DIR}/cpp/tests/shaders/RenderGraphComputeIndirect.frag"
        "${CMAKE_SOURCE_DIR}/cpp/tests/shaders/ShaderReflectionStorage.comp"
        "${CMAKE_SOURCE_DIR}/cpp/tests/shaders/ParticleGpuFixture.comp"
        "${CMAKE_SOURCE_DIR}/cpp/tests/shaders/ParticleGpuBillboard.vert"
        "${CMAKE_SOURCE_DIR}/cpp/tests/shaders/ParticleGpuBillboard.frag"
        "${CMAKE_SOURCE_DIR}/cpp/tests/shaders/ParticleGpuTexturedBillboard.vert"
        "${CMAKE_SOURCE_DIR}/cpp/tests/shaders/ParticleGpuTexturedBillboard.frag"
    )
    set(INFERNUX_COMPUTE_INDIRECT_SHADER_OUTPUTS)
    foreach(_shader_source IN LISTS INFERNUX_COMPUTE_INDIRECT_SHADER_SOURCES)
        get_filename_component(_shader_name "${_shader_source}" NAME)
        set(_shader_output "${INFERNUX_COMPUTE_INDIRECT_SHADER_DIR}/${_shader_name}.spv")
        add_custom_command(
            OUTPUT "${_shader_output}"
            COMMAND ${CMAKE_COMMAND} -E make_directory "${INFERNUX_COMPUTE_INDIRECT_SHADER_DIR}"
            COMMAND "${Vulkan_GLSLANG_VALIDATOR_EXECUTABLE}" -V "${_shader_source}" -o "${_shader_output}"
            DEPENDS "${_shader_source}"
            VERBATIM
        )
        list(APPEND INFERNUX_COMPUTE_INDIRECT_SHADER_OUTPUTS "${_shader_output}")
    endforeach()
    add_custom_target(infernux_render_graph_compute_indirect_shaders
        DEPENDS ${INFERNUX_COMPUTE_INDIRECT_SHADER_OUTPUTS}
    )

    add_executable(infernux_render_graph_compute_indirect_tests
        cpp/tests/RenderGraphComputeIndirectTests.cpp
    )
    add_dependencies(infernux_render_graph_compute_indirect_tests infernux_render_graph_compute_indirect_shaders)
    target_include_directories(infernux_render_graph_compute_indirect_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
        ${CMAKE_SOURCE_DIR}/external/VulkanMemoryAllocator/include
    )
    target_link_libraries(infernux_render_graph_compute_indirect_tests PRIVATE InfernuxRuntime)
    if(MSVC)
        target_compile_options(infernux_render_graph_compute_indirect_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.render_graph_compute_indirect
        COMMAND infernux_render_graph_compute_indirect_tests
            "${INFERNUX_COMPUTE_INDIRECT_SHADER_DIR}/RenderGraphComputeIndirect.comp.spv"
            "${INFERNUX_COMPUTE_INDIRECT_SHADER_DIR}/RenderGraphComputeIndirect.vert.spv"
            "${INFERNUX_COMPUTE_INDIRECT_SHADER_DIR}/RenderGraphComputeIndirect.frag.spv"
            "${INFERNUX_COMPUTE_INDIRECT_SHADER_DIR}/ShaderReflectionStorage.comp.spv"
            "${INFERNUX_COMPUTE_INDIRECT_SHADER_DIR}/ParticleGpuFixture.comp.spv"
            "${INFERNUX_COMPUTE_INDIRECT_SHADER_DIR}/ParticleGpuBillboard.vert.spv"
            "${INFERNUX_COMPUTE_INDIRECT_SHADER_DIR}/ParticleGpuBillboard.frag.spv"
            "${INFERNUX_COMPUTE_INDIRECT_SHADER_DIR}/ParticleGpuTexturedBillboard.vert.spv"
            "${INFERNUX_COMPUTE_INDIRECT_SHADER_DIR}/ParticleGpuTexturedBillboard.frag.spv"
    )
    set_tests_properties(infernux.render_graph_compute_indirect PROPERTIES
        LABELS "requires_vulkan_device"
        TIMEOUT 180
    )

    add_executable(infernux_renderer_list_tests
        cpp/tests/RendererListTests.cpp
    )
    target_include_directories(infernux_renderer_list_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_renderer_list_tests PRIVATE Vulkan::Vulkan glm::glm)
    if(MSVC)
        target_compile_options(infernux_renderer_list_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.renderer_list COMMAND infernux_renderer_list_tests)

    add_executable(infernux_render_world_tests
        cpp/tests/RenderWorldTests.cpp
    )
    target_include_directories(infernux_render_world_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
        ${CMAKE_SOURCE_DIR}/external/VulkanMemoryAllocator/include
    )
    target_link_libraries(infernux_render_world_tests PRIVATE Vulkan::Vulkan glm::glm)
    if(MSVC)
        target_compile_options(infernux_render_world_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.render_world COMMAND infernux_render_world_tests)

    add_executable(infernux_asset_index_tests
        cpp/tests/AssetIndexTests.cpp
        cpp/infernux/core/log/InxLog.cpp
        cpp/infernux/function/resources/AssetDatabase/AssetIndex.cpp
        cpp/infernux/function/resources/InxResource/InxResourceMeta.cpp
        cpp/infernux/core/reflection/InxTypeRegistry.cpp
        cpp/infernux/platform/filesystem/DocumentStore.cpp
        cpp/infernux/platform/filesystem/AtomicFile.cpp
    )
    target_include_directories(infernux_asset_index_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_asset_index_tests PRIVATE Threads::Threads)
    if(MSVC)
        target_compile_options(infernux_asset_index_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.asset_index COMMAND infernux_asset_index_tests)

    add_executable(infernux_asset_database_refresh_tests
        cpp/tests/AssetDatabaseRefreshTests.cpp
    )
    target_include_directories(infernux_asset_database_refresh_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_asset_database_refresh_tests PRIVATE InfernuxRuntime)
    if(MSVC)
        target_compile_options(infernux_asset_database_refresh_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_asset_database_refresh_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.asset_database_refresh COMMAND infernux_asset_database_refresh_tests)

    add_executable(infernux_asset_dependency_graph_tests
        cpp/tests/AssetDependencyGraphTests.cpp
        cpp/infernux/core/log/InxLog.cpp
        cpp/infernux/function/resources/AssetDependencyGraph.cpp
    )
    target_include_directories(infernux_asset_dependency_graph_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_asset_dependency_graph_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.asset_dependency_graph COMMAND infernux_asset_dependency_graph_tests)

    add_executable(infernux_material_document_tests
        cpp/tests/MaterialDocumentTests.cpp
    )
    target_include_directories(infernux_material_document_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_material_document_tests PRIVATE InfernuxRuntime)
    if(MSVC)
        target_compile_options(infernux_material_document_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_material_document_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.material_document COMMAND infernux_material_document_tests)

    add_executable(infernux_mesh_artifact_tests
        cpp/tests/MeshArtifactTests.cpp
        cpp/infernux/core/log/InxLog.cpp
        cpp/infernux/function/resources/InxMesh/MeshArtifact.cpp
        cpp/infernux/function/resources/InxMesh/InxMesh.cpp
        cpp/infernux/function/resources/InxSkinnedMesh/InxSkinnedMesh.cpp
    )
    target_include_directories(infernux_mesh_artifact_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_mesh_artifact_tests PRIVATE Vulkan::Vulkan glm::glm)
    if(MSVC)
        target_compile_options(infernux_mesh_artifact_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.mesh_artifact COMMAND infernux_mesh_artifact_tests)

    add_executable(infernux_skinned_mesh_artifact_tests
        cpp/tests/SkinnedMeshArtifactTests.cpp
        cpp/infernux/core/log/InxLog.cpp
        cpp/infernux/function/resources/InxSkinnedMesh/SkinnedMeshArtifact.cpp
        cpp/infernux/function/resources/InxSkinnedMesh/InxSkinnedMesh.cpp
    )
    target_include_directories(infernux_skinned_mesh_artifact_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_skinned_mesh_artifact_tests PRIVATE Vulkan::Vulkan glm::glm)
    if(MSVC)
        target_compile_options(infernux_skinned_mesh_artifact_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.skinned_mesh_artifact COMMAND infernux_skinned_mesh_artifact_tests)

    add_executable(infernux_texture_artifact_tests
        cpp/tests/TextureArtifactTests.cpp
        cpp/infernux/function/resources/InxTexture/TextureArtifact.cpp
    )
    target_include_directories(infernux_texture_artifact_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_texture_artifact_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.texture_artifact COMMAND infernux_texture_artifact_tests)

    add_executable(infernux_texture_processor_tests
        cpp/tests/TextureProcessorTests.cpp
        cpp/infernux/function/resources/InxTexture/TextureProcessor.cpp
    )
    target_include_directories(infernux_texture_processor_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external/stb
        ${CMAKE_SOURCE_DIR}/external/glm
    )
    if(MSVC)
        target_compile_options(infernux_texture_processor_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.texture_processor COMMAND infernux_texture_processor_tests)

    add_executable(infernux_vector_field_source_tests
        cpp/tests/VectorFieldSourceTests.cpp
        cpp/infernux/function/resources/InxTexture/VectorFieldSource.cpp
    )
    target_include_directories(infernux_vector_field_source_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    if(MSVC)
        target_compile_options(infernux_vector_field_source_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.vector_field_source COMMAND infernux_vector_field_source_tests)

    add_executable(infernux_signed_distance_field_source_tests
        cpp/tests/SignedDistanceFieldSourceTests.cpp
        cpp/infernux/function/resources/InxTexture/SignedDistanceFieldSource.cpp
    )
    target_include_directories(infernux_signed_distance_field_source_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    if(MSVC)
        target_compile_options(infernux_signed_distance_field_source_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.signed_distance_field_source COMMAND infernux_signed_distance_field_source_tests)

    add_executable(infernux_vector_field_importer_tests
        cpp/tests/VectorFieldImporterTests.cpp
    )
    target_include_directories(infernux_vector_field_importer_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_vector_field_importer_tests PRIVATE InfernuxRuntime)
    if(MSVC)
        target_compile_options(infernux_vector_field_importer_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.vector_field_importer COMMAND infernux_vector_field_importer_tests)

    add_executable(infernux_shader_asset_tests
        cpp/tests/ShaderAssetTests.cpp
    )
    target_include_directories(infernux_shader_asset_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_shader_asset_tests PRIVATE InfernuxRuntime)
    if(MSVC)
        target_compile_options(infernux_shader_asset_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_shader_asset_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.shader_asset COMMAND infernux_shader_asset_tests)

    add_executable(infernux_shader_info_schema_tests
        cpp/tests/ShaderInfoSchemaTests.cpp
    )
    target_include_directories(infernux_shader_info_schema_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_shader_info_schema_tests PRIVATE InfernuxRuntime)
    target_compile_definitions(infernux_shader_info_schema_tests PRIVATE
        INFERNUX_TEST_SHADER_ROOT="${CMAKE_SOURCE_DIR}/python/Infernux/resources/shaders"
    )
    if(MSVC)
        target_compile_options(infernux_shader_info_schema_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_shader_info_schema_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.shader_info_schema COMMAND infernux_shader_info_schema_tests)

    add_executable(infernux_shader_stage_linker_tests
        cpp/tests/ShaderStageLinkerTests.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuRibbonRenderer.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuRibbonTopology.cpp
    )
    target_include_directories(infernux_shader_stage_linker_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_shader_stage_linker_tests PRIVATE InfernuxRuntime)
    target_compile_definitions(infernux_shader_stage_linker_tests PRIVATE
        INFERNUX_TEST_SHADER_ROOT="${CMAKE_SOURCE_DIR}/python/Infernux/resources/shaders"
    )
    if(MSVC)
        target_compile_options(infernux_shader_stage_linker_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_shader_stage_linker_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.shader_stage_linker COMMAND infernux_shader_stage_linker_tests)

    add_test(
        NAME infernux.headless
        COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/python/Infernux/test/headless_smoke_test.py
    )

    add_test(
        NAME infernux.bootstrap_import
        COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/python/Infernux/test/bootstrap_import_test.py
    )
    set_tests_properties(infernux.bootstrap_import PROPERTIES
        ENVIRONMENT "INFERNUX_NATIVE_MODULE_DIR=$<TARGET_FILE_DIR:_InfernuxBootstrap>"
    )
    set_tests_properties(infernux.headless PROPERTIES
        ENVIRONMENT "PYTHONPATH=${CMAKE_SOURCE_DIR}/python;INFERNUX_NATIVE_MODULE_DIR=$<TARGET_FILE_DIR:_Infernux>"
        TIMEOUT 30
        RUN_SERIAL TRUE
    )

    add_test(
        NAME infernux.gpu_upload
        COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/python/Infernux/test/gpu_upload_smoke_test.py
    )
    set_tests_properties(infernux.gpu_upload PROPERTIES
        ENVIRONMENT "PYTHONPATH=${CMAKE_SOURCE_DIR}/python;INFERNUX_NATIVE_MODULE_DIR=$<TARGET_FILE_DIR:_Infernux>"
        LABELS "requires_vulkan_device"
        TIMEOUT 30
        RUN_SERIAL TRUE
        SKIP_RETURN_CODE 77
    )

    add_test(
        NAME infernux.scene_residency_soak
        COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/python/Infernux/test/scene_residency_soak_test.py
    )
    set_tests_properties(infernux.scene_residency_soak PROPERTIES
        ENVIRONMENT "PYTHONPATH=${CMAKE_SOURCE_DIR}/python;INFERNUX_NATIVE_MODULE_DIR=$<TARGET_FILE_DIR:_Infernux>"
        LABELS "requires_vulkan_device"
        TIMEOUT 90
        RUN_SERIAL TRUE
        SKIP_RETURN_CODE 77
    )

    # Performance diagnostics are intentionally opt-in. Their wall-clock
    # thresholds depend on the host CPU and filesystem, so they must not make
    # correctness CI fail on slower remote workers.
    add_custom_target(infernux_performance_diagnostics
        COMMAND ${CMAKE_COMMAND} -E env
                "PYTHONPATH=${CMAKE_SOURCE_DIR}/python"
                "INFERNUX_NATIVE_MODULE_DIR=$<TARGET_FILE_DIR:_Infernux>"
                ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/python/Infernux/test/physics_profile_gate.py
        COMMAND ${CMAKE_COMMAND} -E env
                "PYTHONPATH=${CMAKE_SOURCE_DIR}/python"
                "INFERNUX_NATIVE_MODULE_DIR=$<TARGET_FILE_DIR:_Infernux>"
                ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/python/Infernux/test/asset_scan_profile_gate.py
        DEPENDS _Infernux
        USES_TERMINAL
        VERBATIM
    )
endif()

# ------------------------------------------------------------------------------
# Release Pre-Build Cleanup: remove generated Python artifacts recursively
# ------------------------------------------------------------------------------
add_custom_target(clean_python_pycache
    COMMAND ${CMAKE_COMMAND}
            "-DINFERNUX_BUILD_CONFIG=$<CONFIG>"
            -DPYTHON_DIR=${CMAKE_SOURCE_DIR}/python
            -P ${CMAKE_SOURCE_DIR}/cmake/clean_python_pycache.cmake
    COMMENT "Removing __pycache__ directories under python/ for Release"
    VERBATIM
)
add_dependencies(_Infernux _InfernuxBootstrap clean_python_pycache)

# ------------------------------------------------------------------------------
# Post-Build Steps
# ------------------------------------------------------------------------------

# Create a custom script to copy all DLL dependencies
configure_file(
    "${CMAKE_SOURCE_DIR}/copy_dependencies.cmake.in"
    "${CMAKE_BINARY_DIR}/copy_dependencies.cmake"
    @ONLY
)

foreach(_infernux_dll ${INFERNUX_RUNTIME_DLL_TARGETS})
    add_custom_command(TARGET ${_infernux_dll} POST_BUILD
        COMMAND ${CMAKE_COMMAND}
            "-DSOURCE=$<TARGET_FILE:${_infernux_dll}>"
            "-DDESTINATION=${PYTHON_TARGET_DIR}/$<TARGET_FILE_NAME:${_infernux_dll}>"
            -P "${CMAKE_SOURCE_DIR}/cmake/stage_build_file.cmake"
        COMMENT "Copy ${_infernux_dll} shared library to python/Infernux/lib"
        VERBATIM
    )
endforeach()

add_custom_command(TARGET _Infernux POST_BUILD
    # Remove native modules from other Python ABIs/builds first so wheels can
    # never ship two _Infernux modules (GitHub issue #47).
    COMMAND ${CMAKE_COMMAND}
        "-DSOURCE=$<TARGET_FILE:_Infernux>"
        "-DTARGET_DIR=${PYTHON_TARGET_DIR}"
        "-DKEEP_NAME=$<TARGET_FILE_NAME:_Infernux>"
        -P "${CMAKE_SOURCE_DIR}/cmake/stage_native_module.cmake"

    COMMENT "Copy _Infernux binding module to python/Infernux/lib"
)

if(WIN32)
    # Windows: copy all DLL dependencies automatically
    add_custom_command(TARGET _Infernux POST_BUILD
        COMMAND ${CMAKE_COMMAND}
            -DINFERNUX_BUILD_CFG=$<CONFIG>
            -P "${CMAKE_BINARY_DIR}/copy_dependencies.cmake"
        COMMAND ${CMAKE_COMMAND}
            -DINFERNUX_BUILD_CFG=$<CONFIG>
            -DTARGET_DIR=$<TARGET_FILE_DIR:_Infernux>
            -P "${CMAKE_BINARY_DIR}/copy_dependencies.cmake"
        COMMENT "Copy dependent DLLs to package and native module directories"
    )
elseif(APPLE)
    # macOS: copy shared libraries (.dylib) and fix RPATH
    add_custom_command(TARGET _Infernux POST_BUILD
        COMMAND ${CMAKE_COMMAND}
            -DINFERNUX_BUILD_CFG=$<CONFIG>
            -P "${CMAKE_BINARY_DIR}/copy_dependencies.cmake"
        COMMENT "Copy dependent dylibs to python/Infernux/lib"
    )
    # Set RPATH so the module finds dylibs next to itself
    set_target_properties(_Infernux PROPERTIES
        BUILD_RPATH "@loader_path"
        INSTALL_RPATH "@loader_path"
    )
    set_target_properties(_InfernuxBootstrap PROPERTIES
        BUILD_RPATH "@loader_path"
        INSTALL_RPATH "@loader_path"
    )
    foreach(_infernux_dll ${INFERNUX_RUNTIME_DLL_TARGETS})
        set_target_properties(${_infernux_dll} PROPERTIES
            BUILD_RPATH "@loader_path"
            INSTALL_RPATH "@loader_path"
        )
    endforeach()
else()
    # Linux: copy shared libraries (.so) and set RPATH
    add_custom_command(TARGET _Infernux POST_BUILD
        COMMAND ${CMAKE_COMMAND}
            -DINFERNUX_BUILD_CFG=$<CONFIG>
            -P "${CMAKE_BINARY_DIR}/copy_dependencies.cmake"
        COMMENT "Copy dependent shared libs to python/Infernux/lib"
    )
    set_target_properties(_Infernux PROPERTIES
        BUILD_RPATH "$ORIGIN"
        INSTALL_RPATH "$ORIGIN"
    )
    set_target_properties(_InfernuxBootstrap PROPERTIES
        BUILD_RPATH "$ORIGIN"
        INSTALL_RPATH "$ORIGIN"
    )
    foreach(_infernux_dll ${INFERNUX_RUNTIME_DLL_TARGETS})
        set_target_properties(${_infernux_dll} PROPERTIES
            BUILD_RPATH "$ORIGIN"
            INSTALL_RPATH "$ORIGIN"
        )
    endforeach()
endif()

# ------------------------------------------------------------------------------
# Wheel-distributed Player Runtime Pack
# ------------------------------------------------------------------------------
# This is an explicit stage in the package_and_install_python dependency chain.
# The script exits immediately for non-Release configurations (for example
# RelWithDebInfo), so debug installs still package without building a Player
# Runtime Pack.
set(INFERNUX_PREBUILT_RUNTIME_DIR "${CMAKE_SOURCE_DIR}/python/Infernux/_runtime_packs")
set(INFERNUX_PREBUILT_RUNTIME_MODULE_DIR "${CMAKE_SOURCE_DIR}/python/Infernux/_runtime_modules")
add_custom_target(prebuild_player_runtime
    COMMAND ${CMAKE_COMMAND}
        "-DINFERNUX_BUILD_CONFIG=$<CONFIG>"
        "-DINFERNUX_SOURCE_DIR=${CMAKE_SOURCE_DIR}"
        "-DPYTHON_EXECUTABLE=${Python3_EXECUTABLE}"
        "-DNATIVE_MODULE_DIR=$<TARGET_FILE_DIR:_Infernux>"
        "-DOUTPUT_ROOT=${INFERNUX_PREBUILT_RUNTIME_DIR}"
        "-DMODULE_OUTPUT_ROOT=${INFERNUX_PREBUILT_RUNTIME_MODULE_DIR}"
        -P "${CMAKE_SOURCE_DIR}/cmake/prebuild_player_runtime.cmake"
    COMMAND ${CMAKE_COMMAND}
        "-DINFERNUX_BUILD_CONFIG=$<CONFIG>"
        "-DPYTHON_DIR=${CMAKE_SOURCE_DIR}/python"
        -P "${CMAKE_SOURCE_DIR}/cmake/clean_python_pycache.cmake"
    COMMAND ${CMAKE_COMMAND}
        "-DINFERNUX_BUILD_CONFIG=$<CONFIG>"
        "-DPYTHON_DIR=${CMAKE_SOURCE_DIR}/packaging"
        -P "${CMAKE_SOURCE_DIR}/cmake/clean_python_pycache.cmake"
    DEPENDS _Infernux _InfernuxBootstrap
    WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
    COMMENT "Preparing wheel-distributed Release Player Runtime Pack and parallel module"
    VERBATIM
)
if(TARGET InfernuxPlayerHost)
    add_dependencies(prebuild_player_runtime InfernuxPlayerHost)
endif()

# Audit an already-built Player directory without making game packaging part
# of the normal engine/wheel build.  This target is intentionally explicit:
# configure with -DINFERNUX_PLAYER_OUTPUT_DIR=<path> or pass it at build time.
set(INFERNUX_PLAYER_OUTPUT_DIR "" CACHE PATH
    "Existing Player output directory to audit")
add_custom_target(player_package_audit
    COMMAND ${CMAKE_COMMAND}
        "-DINFERNUX_SOURCE_DIR=${CMAKE_SOURCE_DIR}"
        "-DPYTHON_EXECUTABLE=${Python3_EXECUTABLE}"
        "-DINFERNUX_PLAYER_OUTPUT_DIR=${INFERNUX_PLAYER_OUTPUT_DIR}"
        -P "${CMAKE_SOURCE_DIR}/cmake/verify_player_package.cmake"
    WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
    COMMENT "Auditing Player service manifest, source privacy and payload uniqueness"
    VERBATIM
)

add_custom_command(TARGET _InfernuxBootstrap POST_BUILD
    COMMAND ${CMAKE_COMMAND}
        "-DSOURCE=$<TARGET_FILE:_InfernuxBootstrap>"
        "-DTARGET_DIR=${PYTHON_TARGET_DIR}"
        "-DKEEP_NAME=$<TARGET_FILE_NAME:_InfernuxBootstrap>"
        -P "${CMAKE_SOURCE_DIR}/cmake/stage_native_module.cmake"
    COMMENT "Copy _InfernuxBootstrap binding module to python/Infernux/lib"
    VERBATIM
)

# ------------------------------------------------------------------------------
# Infernux Hub standalone packaging targets
# ------------------------------------------------------------------------------
include("${CMAKE_SOURCE_DIR}/cmake/InfernuxHub.cmake")

# ------------------------------------------------------------------------------
# Code Formatting Target (clang-format)
# ------------------------------------------------------------------------------
find_program(INFERNUX_CLANG_FORMAT_EXECUTABLE
    NAMES clang-format clang-format-22
    REQUIRED
)

add_custom_target(clang_format
    COMMAND "${Python3_EXECUTABLE}"
        "${CMAKE_SOURCE_DIR}/cmake/run_clang_format.py"
        --clang-format "${INFERNUX_CLANG_FORMAT_EXECUTABLE}"
        --source-root "${CMAKE_SOURCE_DIR}/cpp"
    DEPENDS "${CMAKE_SOURCE_DIR}/cmake/run_clang_format.py"
    COMMENT "Running clang-format on source files"
    VERBATIM
)

# ------------------------------------------------------------------------------
# Python Packaging and Installation
# ------------------------------------------------------------------------------
set(INFERNUX_PYTHON_WHEEL_DIR "${CMAKE_BINARY_DIR}/python_wheel")

add_custom_target(package_and_install_python
    COMMAND ${CMAKE_COMMAND} -E echo "Ensuring Python packaging tools are available..."
    COMMAND ${CMAKE_COMMAND}
        -DINFERNUX_SOURCE_DIR=${CMAKE_SOURCE_DIR}
        -DPYTHON_EXECUTABLE=${Python3_EXECUTABLE}
        -P "${CMAKE_SOURCE_DIR}/cmake/ensure_python_packaging_tools.cmake"

    COMMAND ${CMAKE_COMMAND} -E echo "Building Python wheel via '${Python3_EXECUTABLE} -m build --wheel'..."
    # setuptools reuses python package files from this staging directory when
    # their timestamps look current. Native dependencies can otherwise come
    # from a previous CMake configuration (for example Release Jolt.dll beside
    # a RelWithDebInfo _Infernux.pyd).
    COMMAND ${CMAKE_COMMAND} -E rm -rf "${CMAKE_SOURCE_DIR}/build"
    COMMAND ${CMAKE_COMMAND} -E rm -rf "${CMAKE_SOURCE_DIR}/python/Infernux.egg-info"
    COMMAND ${CMAKE_COMMAND} -E rm -rf "${INFERNUX_PYTHON_WHEEL_DIR}"
    COMMAND ${CMAKE_COMMAND} -E make_directory "${INFERNUX_PYTHON_WHEEL_DIR}"
    COMMAND ${CMAKE_COMMAND} -E env
        "INFERNUX_SOURCE_DIR=${CMAKE_SOURCE_DIR}"
        "${Python3_EXECUTABLE}" -m build --wheel --no-isolation --outdir "${INFERNUX_PYTHON_WHEEL_DIR}"

    COMMAND ${CMAKE_COMMAND} -E echo "Verifying native wheel payload against the current build..."
    COMMAND ${CMAKE_COMMAND}
        -DINFERNUX_SOURCE_DIR=${CMAKE_SOURCE_DIR}
        -DINFERNUX_WHEEL_DIR=${INFERNUX_PYTHON_WHEEL_DIR}
        -P "${CMAKE_SOURCE_DIR}/cmake/verify_python_wheel.cmake"

    COMMAND ${CMAKE_COMMAND} -E echo "Removing build metadata before wheel installation..."
    COMMAND ${CMAKE_COMMAND} -E rm -rf "${CMAKE_SOURCE_DIR}/python/Infernux.egg-info"

    COMMAND ${CMAKE_COMMAND} -E echo "Installing built wheel via pip..."
    COMMAND ${CMAKE_COMMAND}
        -DINFERNUX_SOURCE_DIR=${CMAKE_SOURCE_DIR}
        -DINFERNUX_WHEEL_DIR=${INFERNUX_PYTHON_WHEEL_DIR}
        -DPYTHON_EXECUTABLE=${Python3_EXECUTABLE}
        -P "${CMAKE_SOURCE_DIR}/install_wheel.cmake"

    COMMAND ${CMAKE_COMMAND} -E echo "Cleaning up .egg-info directory..."
    COMMAND ${CMAKE_COMMAND} -E rm -rf "${CMAKE_SOURCE_DIR}/python/Infernux.egg-info"

    COMMAND ${CMAKE_COMMAND} -E echo "Cleaning Python build artifacts after packaging..."
    COMMAND ${CMAKE_COMMAND}
            "-DINFERNUX_BUILD_CONFIG=$<CONFIG>"
            -DPYTHON_DIR=${CMAKE_SOURCE_DIR}/python
            -P ${CMAKE_SOURCE_DIR}/cmake/clean_python_pycache.cmake
    COMMAND ${CMAKE_COMMAND}
            "-DINFERNUX_BUILD_CONFIG=$<CONFIG>"
            -DPYTHON_DIR=${CMAKE_SOURCE_DIR}/packaging
            -P ${CMAKE_SOURCE_DIR}/cmake/clean_python_pycache.cmake

    WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
    COMMENT "Python packaging and installation after native module build"
)

# Keep packaging behind one ordered dependency chain. The native Python
# modules already depend on clean_python_pycache; adding cleanup here as a
# second root allows it to race runtime staging under parallel MSBuild.
add_dependencies(package_and_install_python prebuild_player_runtime)

# ------------------------------------------------------------------------------
# API Reference Generation (auto-introspect Python package → Markdown)
# ------------------------------------------------------------------------------
add_custom_target(generate_api_docs
    COMMAND ${CMAKE_COMMAND} -E echo "Generating API reference docs..."
    COMMAND ${CMAKE_COMMAND} -E env PYTHONIOENCODING=utf-8
            python "${CMAKE_SOURCE_DIR}/docs/wiki/generate_api_docs.py"
    COMMENT "Auto-generate API reference Markdown from Python stubs"
)

# ------------------------------------------------------------------------------
# Wiki Build (MkDocs)
# ------------------------------------------------------------------------------
add_custom_target(build_wiki_html
    COMMAND ${CMAKE_COMMAND} -E echo "Building MkDocs wiki..."
    COMMAND ${CMAKE_COMMAND} -E env PYTHONIOENCODING=utf-8
            python -m mkdocs build --clean -f "${CMAKE_SOURCE_DIR}/docs/wiki/mkdocs.yml"
    COMMENT "Generate wiki HTML into docs/wiki/site"
)
add_dependencies(build_wiki_html generate_api_docs)
