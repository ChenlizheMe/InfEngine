cmake_minimum_required(VERSION 3.25)

project(InfernuxWebPlayerHost LANGUAGES C CXX)

if(NOT EMSCRIPTEN)
    message(FATAL_ERROR "InfernuxWebPlayerHost must be configured with emcmake")
endif()

set(INFERNUX_WEB_CPYTHON_SOURCE "" CACHE PATH
    "CPython 3.13 source directory")
set(INFERNUX_WEB_CPYTHON_BUILD "" CACHE PATH
    "Configured CPython 3.13 emscripten-browser build directory")
set(INFERNUX_ENGINE_SOURCE_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../../../.." CACHE PATH
    "Infernux source directory providing the backend-neutral RHI contract")
set(INFERNUX_WEB_PLAYER_ASSETS "" CACHE PATH
    "Optional cooked project and Python assets to preload into the Web host")
set(INFERNUX_WEB_ASSET_REVISION "" CACHE STRING
    "Deterministic identity shared by the Web Player JavaScript and data files")
string(LENGTH "${INFERNUX_WEB_ASSET_REVISION}" _web_asset_revision_length)
if(NOT INFERNUX_WEB_ASSET_REVISION MATCHES "^[0-9a-f]+$" OR
   NOT _web_asset_revision_length EQUAL 24)
    message(FATAL_ERROR
        "INFERNUX_WEB_ASSET_REVISION must be a 24-character lowercase SHA-256 prefix")
endif()
option(INFERNUX_WEB_BUILD_NATIVE_MODULE_PROBE
    "Compile the existing Infernux Python binding surface for wasm without linking it"
    OFF
)
option(INFERNUX_WEB_LINK_NATIVE_MODULE_PROBE
    "Link the Web Player runtime binding and scene inventories into the host"
    OFF
)
option(INFERNUX_WEB_LINK_ENGINE_RUNTIME
    "Statically link the supported Infernux gameplay runtime into the Web Player"
    OFF
)
if(INFERNUX_WEB_LINK_NATIVE_MODULE_PROBE AND
   NOT INFERNUX_WEB_BUILD_NATIVE_MODULE_PROBE)
    message(FATAL_ERROR
        "INFERNUX_WEB_LINK_NATIVE_MODULE_PROBE requires INFERNUX_WEB_BUILD_NATIVE_MODULE_PROBE")
endif()

foreach(_required
        INFERNUX_WEB_CPYTHON_SOURCE
        INFERNUX_WEB_CPYTHON_BUILD
        INFERNUX_ENGINE_SOURCE_ROOT)
    if(NOT IS_DIRECTORY "${${_required}}")
        message(FATAL_ERROR "${_required} must name an existing directory")
    endif()
endforeach()

set(_python_library "${INFERNUX_WEB_CPYTHON_BUILD}/libpython3.13.a")
set(_python_assets "${INFERNUX_WEB_CPYTHON_BUILD}/usr/local")
set(_python_expat "${INFERNUX_WEB_CPYTHON_BUILD}/Modules/expat/libexpat.a")
set(_python_mpdecimal
    "${INFERNUX_WEB_CPYTHON_BUILD}/Modules/_decimal/libmpdec/libmpdec.a")
set(_python_hacl
    "${INFERNUX_WEB_CPYTHON_BUILD}/Modules/_hacl/libHacl_Hash_SHA2.a")
if(NOT EXISTS "${_python_library}")
    message(FATAL_ERROR "Missing CPython wasm library: ${_python_library}")
endif()
if(NOT IS_DIRECTORY "${_python_assets}")
    message(FATAL_ERROR "Missing CPython wasm standard library: ${_python_assets}")
endif()
foreach(_archive _python_expat _python_mpdecimal _python_hacl)
    if(NOT EXISTS "${${_archive}}")
        message(FATAL_ERROR "Missing CPython wasm dependency: ${${_archive}}")
    endif()
endforeach()

include(FetchContent)
set(ZSTD_BUILD_PROGRAMS OFF CACHE BOOL "" FORCE)
set(ZSTD_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(ZSTD_BUILD_CONTRIB OFF CACHE BOOL "" FORCE)
set(ZSTD_BUILD_SHARED OFF CACHE BOOL "" FORCE)
set(ZSTD_BUILD_STATIC ON CACHE BOOL "" FORCE)
set(ZSTD_MULTITHREAD_SUPPORT OFF CACHE BOOL "" FORCE)
set(INFERNUX_WEB_ZSTD_SOURCE_DIR "" CACHE PATH
    "Optional pinned Zstandard source tree for offline Web Player builds")
if(INFERNUX_WEB_ZSTD_SOURCE_DIR)
    if(NOT EXISTS "${INFERNUX_WEB_ZSTD_SOURCE_DIR}/build/cmake/CMakeLists.txt")
        message(FATAL_ERROR
            "INFERNUX_WEB_ZSTD_SOURCE_DIR must contain build/cmake/CMakeLists.txt")
    endif()
    add_subdirectory(
        "${INFERNUX_WEB_ZSTD_SOURCE_DIR}/build/cmake"
        "${CMAKE_BINARY_DIR}/_deps/infernux-web-zstd-local"
        EXCLUDE_FROM_ALL
    )
else()
    FetchContent_Declare(
        infernux_web_zstd
        GIT_REPOSITORY https://github.com/facebook/zstd.git
        GIT_TAG 35016bc1c0b9a2f7121b7ecc312100aad7d9f2ad
        SOURCE_SUBDIR build/cmake
    )
    FetchContent_MakeAvailable(infernux_web_zstd)
endif()
if(TARGET libzstd_static)
    set(_web_zstd_target libzstd_static)
elseif(TARGET zstd::libzstd_static)
    set(_web_zstd_target zstd::libzstd_static)
else()
    message(FATAL_ERROR "The pinned Web Player Zstandard source did not provide a static library")
endif()

add_executable(InfernuxWebPlayerHost
    main.cpp
    InfernuxWebHostModule.cpp
    WebGpuRhiDevice.cpp
    WebParticleRuntime.cpp
    WebSceneRenderer.cpp
    "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/function/renderer/particle/ParticleGpuContactRuntime.cpp"
    "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/function/renderer/particle/ParticleGpuContinuationRuntime.cpp"
    "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/function/renderer/particle/ParticleGpuRuntime.cpp"
    "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/function/renderer/FullscreenRenderer.cpp"
    "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/function/renderer/rhi/RhiDeviceIdentity.cpp"
    "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/platform/filesystem/InxPack.cpp"
    "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/platform/input/InputManager.cpp"
)
set_target_properties(InfernuxWebPlayerHost PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
    OUTPUT_NAME "infernux-player"
    SUFFIX ".html"
)
set_property(TARGET InfernuxWebPlayerHost APPEND PROPERTY LINK_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/bootstrap.py"
    "${CMAKE_CURRENT_SOURCE_DIR}/shell.html"
)
configure_file(
    "${CMAKE_CURRENT_SOURCE_DIR}/shell.html"
    "${CMAKE_CURRENT_BINARY_DIR}/shell.generated.html"
    @ONLY
)

target_include_directories(InfernuxWebPlayerHost PRIVATE
    "${INFERNUX_WEB_CPYTHON_SOURCE}/Include"
    "${INFERNUX_WEB_CPYTHON_BUILD}"
    "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux"
    "${INFERNUX_ENGINE_SOURCE_ROOT}/external"
    "${INFERNUX_ENGINE_SOURCE_ROOT}/external/glm"
    "${INFERNUX_ENGINE_SOURCE_ROOT}/external/nlohmann/single_include"
    "${INFERNUX_ENGINE_SOURCE_ROOT}/external/SDL/include"
    "${INFERNUX_ENGINE_SOURCE_ROOT}/external/stb"
)
target_link_libraries(InfernuxWebPlayerHost PRIVATE
    "${_python_library}"
    "${_python_expat}"
    "${_python_mpdecimal}"
    "${_python_hacl}"
    ${_web_zstd_target}
    dl
    pthread
    m
)
target_compile_definitions(InfernuxWebPlayerHost PRIVATE
    GLM_FORCE_DEPTH_ZERO_TO_ONE
    GLM_FORCE_LEFT_HANDED
    INFERNUX_DISABLE_VULKAN_MATERIAL_RUNTIME=1
    INFERNUX_INPUT_SEMANTIC_HOST=1
    INFERNUX_RUNTIME_MINIMAL_HOST=1
    INFERNUX_SINGLE_THREADED_RUNTIME=1
    INFERNUX_WEBGPU_RUNTIME=1
)

if(INFERNUX_WEB_BUILD_NATIVE_MODULE_PROBE OR
   INFERNUX_WEB_LINK_ENGINE_RUNTIME)
    execute_process(
        COMMAND python -c "import pybind11; print(pybind11.get_include())"
        OUTPUT_VARIABLE _web_pybind11_include
        OUTPUT_STRIP_TRAILING_WHITESPACE
        RESULT_VARIABLE _web_pybind11_result
    )
    if(NOT _web_pybind11_result EQUAL 0 OR
       NOT EXISTS "${_web_pybind11_include}/pybind11/pybind11.h")
        message(FATAL_ERROR
            "The active Web build environment must provide pybind11 headers")
    endif()
    file(GLOB _web_binding_sources CONFIGURE_DEPENDS
        "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/tools/pybinding/*.cpp"
    )
    list(FILTER _web_binding_sources EXCLUDE REGEX "BindingInfernuxBootstrap\\.cpp$")
    set(_web_portable_binding_sources ${_web_binding_sources})
    list(FILTER _web_portable_binding_sources EXCLUDE REGEX "BindingInfernux\\.cpp$")
    list(FILTER _web_portable_binding_sources EXCLUDE REGEX "BindingGUI\\.cpp$")
    list(FILTER _web_portable_binding_sources EXCLUDE REGEX "BindingRenderGraph\\.cpp$")
    set(_web_runtime_binding_sources
        "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/tools/pybinding/BindingModule.cpp"
        "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/tools/pybinding/BindingAssetRegistry.cpp"
        "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/tools/pybinding/BindingBatch.cpp"
        "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/tools/pybinding/BindingInput.cpp"
        "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/tools/pybinding/BindingPhysics.cpp"
        "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/tools/pybinding/BindingResources.cpp"
        "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/tools/pybinding/BindingScene.cpp"
        "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/tools/pybinding/BindingTagLayer.cpp"
        "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/tools/pybinding/BindingVector2f.cpp"
        "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/tools/pybinding/BindingVector3f.cpp"
        "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/tools/pybinding/BindingVector4f.cpp"
    )

    add_library(InfernuxWebNativeModuleProbe OBJECT EXCLUDE_FROM_ALL
        ${_web_binding_sources}
    )
    add_library(InfernuxWebPortableBindingsProbe OBJECT EXCLUDE_FROM_ALL
        ${_web_portable_binding_sources}
    )
    add_library(InfernuxWebRuntimeBindingsProbe OBJECT EXCLUDE_FROM_ALL
        ${_web_runtime_binding_sources}
    )
    file(GLOB _web_scene_runtime_sources CONFIGURE_DEPENDS
        "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/function/scene/*.cpp"
        "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/function/scene/physics/*.cpp"
    )
    list(FILTER _web_scene_runtime_sources EXCLUDE REGEX "EditorCameraController\\.cpp$")
    list(FILTER _web_scene_runtime_sources EXCLUDE REGEX "SceneRenderBridge\\.cpp$")
    list(FILTER _web_scene_runtime_sources EXCLUDE REGEX "SceneRenderer\\.cpp$")
    add_library(InfernuxWebSceneRuntimeProbe OBJECT EXCLUDE_FROM_ALL
        ${_web_scene_runtime_sources}
    )
    file(GLOB_RECURSE _web_resource_runtime_sources CONFIGURE_DEPENDS
        "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/core/*.cpp"
        "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/function/resources/*.cpp"
        "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/platform/filesystem/*.cpp"
    )
    list(FILTER _web_resource_runtime_sources EXCLUDE REGEX "/AssetImporter/")
    list(FILTER _web_resource_runtime_sources EXCLUDE REGEX "SkinnedModelImporter\\.cpp$")
    list(FILTER _web_resource_runtime_sources EXCLUDE REGEX "/InxMesh/MeshLoader\\.cpp$")
    list(FILTER _web_resource_runtime_sources EXCLUDE REGEX "TextureProcessor\\.cpp$")
    list(FILTER _web_resource_runtime_sources EXCLUDE REGEX "InxShaderLoader\\.cpp$")
    list(FILTER _web_resource_runtime_sources EXCLUDE REGEX "InxPack\\.cpp$")
    add_library(InfernuxWebResourceRuntimeProbe OBJECT EXCLUDE_FROM_ALL
        ${_web_resource_runtime_sources}
    )
    add_library(InfernuxWebPlatformRuntimeProbe OBJECT EXCLUDE_FROM_ALL
        "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux/platform/input/InputManager.cpp"
    )
    foreach(_web_binding_probe
            InfernuxWebNativeModuleProbe
            InfernuxWebPortableBindingsProbe
            InfernuxWebRuntimeBindingsProbe
            InfernuxWebSceneRuntimeProbe
            InfernuxWebResourceRuntimeProbe
            InfernuxWebPlatformRuntimeProbe)
        target_include_directories(${_web_binding_probe} PRIVATE
            "${_web_pybind11_include}"
            "${INFERNUX_WEB_CPYTHON_SOURCE}/Include"
            "${INFERNUX_WEB_CPYTHON_BUILD}"
            "${INFERNUX_ENGINE_SOURCE_ROOT}/cpp/infernux"
            "${INFERNUX_ENGINE_SOURCE_ROOT}/external"
            "${INFERNUX_ENGINE_SOURCE_ROOT}/external/assimp/include"
            "${INFERNUX_ENGINE_SOURCE_ROOT}/external/glm"
            "${INFERNUX_ENGINE_SOURCE_ROOT}/external/glslang"
            "${INFERNUX_ENGINE_SOURCE_ROOT}/external/imgui"
            "${INFERNUX_ENGINE_SOURCE_ROOT}/external/JoltPhysics"
            "${INFERNUX_ENGINE_SOURCE_ROOT}/external/nlohmann/single_include"
            "${INFERNUX_ENGINE_SOURCE_ROOT}/external/SDL/include"
            "${INFERNUX_ENGINE_SOURCE_ROOT}/external/stb"
        )
        target_compile_definitions(${_web_binding_probe} PRIVATE
            GLM_FORCE_DEPTH_ZERO_TO_ONE
            GLM_FORCE_LEFT_HANDED
            INFERNUX_DISABLE_VULKAN_MATERIAL_RUNTIME=1
            INFERNUX_INPUT_SEMANTIC_HOST=1
            INFERNUX_RUNTIME_MINIMAL_HOST=1
            INFERNUX_SINGLE_THREADED_RUNTIME=1
            JPH_USE_CPU_COMPUTE
            JPH_OBJECT_STREAM
        )
    endforeach()
    target_compile_definitions(InfernuxWebRuntimeBindingsProbe PRIVATE
        INFERNUX_PYBIND_WEB_PLAYER=1
    )
    if(INFERNUX_WEB_LINK_NATIVE_MODULE_PROBE OR
       INFERNUX_WEB_LINK_ENGINE_RUNTIME)
        set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
        set(USE_SSE4_1 OFF CACHE BOOL "" FORCE)
        set(USE_SSE4_2 OFF CACHE BOOL "" FORCE)
        set(USE_AVX OFF CACHE BOOL "" FORCE)
        set(USE_AVX2 OFF CACHE BOOL "" FORCE)
        set(USE_AVX512 OFF CACHE BOOL "" FORCE)
        set(USE_LZCNT OFF CACHE BOOL "" FORCE)
        set(USE_TZCNT OFF CACHE BOOL "" FORCE)
        set(USE_F16C OFF CACHE BOOL "" FORCE)
        set(USE_FMADD OFF CACHE BOOL "" FORCE)
        set(OVERRIDE_CXX_FLAGS OFF CACHE BOOL "" FORCE)
        set(INTERPROCEDURAL_OPTIMIZATION OFF CACHE BOOL "" FORCE)
        set(DOUBLE_PRECISION OFF CACHE BOOL "" FORCE)
        set(GENERATE_DEBUG_SYMBOLS OFF CACHE BOOL "" FORCE)
        set(ENABLE_ALL_WARNINGS OFF CACHE BOOL "" FORCE)
        set(ENABLE_OBJECT_STREAM ON CACHE BOOL "" FORCE)
        set(ENABLE_INSTALL OFF CACHE BOOL "" FORCE)
        set(CPP_EXCEPTIONS_ENABLED ON CACHE BOOL "" FORCE)
        set(CPP_RTTI_ENABLED ON CACHE BOOL "" FORCE)
        set(DEBUG_RENDERER_IN_DEBUG_AND_RELEASE OFF CACHE BOOL "" FORCE)
        set(PROFILER_IN_DEBUG_AND_RELEASE OFF CACHE BOOL "" FORCE)
        set(JPH_USE_DX12 OFF CACHE BOOL "" FORCE)
        set(JPH_USE_VK OFF CACHE BOOL "" FORCE)
        set(JPH_USE_MTL OFF CACHE BOOL "" FORCE)
        set(JPH_USE_CPU_COMPUTE ON CACHE BOOL "" FORCE)
        add_subdirectory(
            "${INFERNUX_ENGINE_SOURCE_ROOT}/external/JoltPhysics/Build"
            "${CMAKE_CURRENT_BINARY_DIR}/_deps/infernux-web-jolt"
            EXCLUDE_FROM_ALL
        )
        target_sources(InfernuxWebPlayerHost PRIVATE
            $<TARGET_OBJECTS:InfernuxWebRuntimeBindingsProbe>
            $<TARGET_OBJECTS:InfernuxWebSceneRuntimeProbe>
            $<TARGET_OBJECTS:InfernuxWebResourceRuntimeProbe>
        )
        target_compile_definitions(InfernuxWebPlayerHost PRIVATE
            INFERNUX_WEB_ENGINE_RUNTIME=1
        )
        target_link_libraries(InfernuxWebPlayerHost PRIVATE Jolt)
    endif()
endif()

target_compile_options(InfernuxWebPlayerHost PRIVATE
    "--use-port=emdawnwebgpu"
    "-sUSE_ZLIB=1"
    "-sUSE_BZIP2=1"
    "-sUSE_SQLITE3=1"
)
target_link_options(InfernuxWebPlayerHost PRIVATE
    "--use-port=emdawnwebgpu"
    "-sUSE_ZLIB=1"
    "-sUSE_BZIP2=1"
    "-sUSE_SQLITE3=1"
    "-sALLOW_MEMORY_GROWTH=1"
    "-sINITIAL_MEMORY=134217728"
    "-sMAXIMUM_MEMORY=2147483648"
    "-sFORCE_FILESYSTEM=1"
    "-sENVIRONMENT=web"
    "-sEXIT_RUNTIME=0"
    "-sEXPORTED_RUNTIME_METHODS=['ccall']"
    "--shell-file=${CMAKE_CURRENT_BINARY_DIR}/shell.generated.html"
    "--preload-file=${_python_assets}@/usr/local"
    "--preload-file=${CMAKE_CURRENT_SOURCE_DIR}/bootstrap.py@/infernux/bootstrap.py"
)
if(INFERNUX_WEB_PLAYER_ASSETS)
    if(NOT IS_DIRECTORY "${INFERNUX_WEB_PLAYER_ASSETS}")
        message(FATAL_ERROR
            "INFERNUX_WEB_PLAYER_ASSETS must name an existing directory")
    endif()
    file(GLOB_RECURSE _web_player_asset_files CONFIGURE_DEPENDS
        "${INFERNUX_WEB_PLAYER_ASSETS}/*"
    )
    set_property(TARGET InfernuxWebPlayerHost APPEND PROPERTY LINK_DEPENDS
        ${_web_player_asset_files}
    )
    target_link_options(InfernuxWebPlayerHost PRIVATE
        "--preload-file=${INFERNUX_WEB_PLAYER_ASSETS}@/infernux/player"
    )
endif()

add_custom_command(TARGET InfernuxWebPlayerHost POST_BUILD
    COMMAND "${CMAKE_COMMAND}"
        "-DINFERNUX_WEB_HTML=$<TARGET_FILE:InfernuxWebPlayerHost>"
        "-DINFERNUX_WEB_ASSET_REVISION=${INFERNUX_WEB_ASSET_REVISION}"
        -P "${CMAKE_CURRENT_SOURCE_DIR}/stamp_asset_revision.cmake"
    VERBATIM
)
