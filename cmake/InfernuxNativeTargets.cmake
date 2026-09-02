# Native engine libraries and Python extension modules.

# NO_EXTRAS keeps pybind11 from choosing its own LTO flags. Release IPO/LTO is
# enabled explicitly below where the target's export strategy supports it, so
# compile and link settings always agree.
add_library(InfernuxFoundation SHARED ${INFERNUX_FOUNDATION_SOURCES})
add_library(InfernuxParticleRuntime SHARED ${INFERNUX_PARTICLE_RUNTIME_SOURCES})
add_library(InfernuxShaderCompiler SHARED ${INFERNUX_SHADER_COMPILER_SOURCES})
add_library(InfernuxRenderCore SHARED ${INFERNUX_RENDER_CORE_SOURCES})
add_library(InfernuxRendererRuntime SHARED ${INFERNUX_RENDERER_RUNTIME_SOURCES})
add_library(InfernuxVulkanBackend SHARED ${INFERNUX_VULKAN_BACKEND_SOURCES})

set(_infernux_vulkan_loader Vulkan::Vulkan)
if(INFERNUX_VULKAN_LOADER_TARGET)
    set(_infernux_vulkan_loader ${INFERNUX_VULKAN_LOADER_TARGET})
endif()

# Shipping builds keep the composition layer as a private archive so the Python
# module can LTO/dead-strip the exact bound surface instead of shipping a
# 60k-symbol auto-export DLL. Native regression tests use the shared form to
# avoid loading a full private runtime image into every test process.
if(INFERNUX_RUNTIME_STATIC)
    add_library(InfernuxRuntime STATIC ${INFERNUX_RUNTIME_SOURCES})
else()
    add_library(InfernuxRuntime SHARED ${INFERNUX_RUNTIME_SOURCES})
endif()

if(INFERNUX_USE_TARGET_PYTHON)
    add_library(_Infernux MODULE ${BINDING_SOURCES})
    add_library(_InfernuxBootstrap MODULE ${INFERNUX_BOOTSTRAP_BINDING_SOURCE})
    foreach(_infernux_python_module _Infernux _InfernuxBootstrap)
        set_target_properties(${_infernux_python_module} PROPERTIES PREFIX "")
        target_link_libraries(${_infernux_python_module} PRIVATE
            pybind11::headers
            InfernuxTargetPython
        )
    endforeach()
else()
    pybind11_add_module(_Infernux MODULE NO_EXTRAS ${BINDING_SOURCES})
    pybind11_add_module(_InfernuxBootstrap MODULE NO_EXTRAS ${INFERNUX_BOOTSTRAP_BINDING_SOURCE})
endif()

# Shared subsystem boundaries still predate explicit visibility annotations.
# The composition library is private to the Python module in shipping builds.
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
if(INFERNUX_VULKAN_LOADER_TARGET)
    foreach(_infernux_target ${INFERNUX_NATIVE_TARGETS})
        target_compile_definitions(${_infernux_target} PRIVATE
            VK_NO_PROTOTYPES=1
            INFERNUX_USE_VOLK=1
        )
        target_compile_options(${_infernux_target} PRIVATE
            "$<$<COMPILE_LANGUAGE:C,CXX>:-include>"
            "$<$<COMPILE_LANGUAGE:C,CXX>:volk.h>"
        )
    endforeach()
endif()

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
if(INFERNUX_USE_TARGET_PYTHON)
    target_link_libraries(InfernuxRuntime PRIVATE pybind11::headers InfernuxTargetPython)
else()
    # InfernuxRuntime is ultimately loaded through the _Infernux extension.
    # Python3::Module links the import library where a platform requires it
    # (Windows), but deliberately leaves Python symbols for the interpreter on
    # ELF platforms. pybind11::embed would add a DT_NEEDED libpython entry to
    # the wheel and make an otherwise valid venv depend on its creator prefix.
    target_link_libraries(InfernuxRuntime PRIVATE pybind11::headers Python3::Module)
endif()
target_link_libraries(_Infernux PRIVATE InfernuxRuntime)
target_link_libraries(_InfernuxBootstrap PRIVATE InfernuxFoundation)
if(WIN32)
    target_link_libraries(_InfernuxBootstrap PRIVATE user32)
endif()

if(MSVC)
    # MSBuild already compiles independent projects concurrently. Bound /MP and
    # serialize writes to the shared compiler PDB.
    foreach(_infernux_target ${INFERNUX_NATIVE_TARGETS})
        target_compile_options(${_infernux_target} PRIVATE
            /utf-8
            /MP
            /FS
            /bigobj
        )
    endforeach()
elseif(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
    foreach(_infernux_target InfernuxRuntime _Infernux)
        target_compile_options(${_infernux_target} PRIVATE
            $<$<CONFIG:Release>:-O2>
        )
    endforeach()
endif()

if(INFERNUX_RELEASE_LTO)
    include(CheckIPOSupported)
    check_ipo_supported(RESULT _infernux_ipo_supported OUTPUT _infernux_ipo_error)
    if(_infernux_ipo_supported)
        set_property(TARGET _Infernux PROPERTY INTERPROCEDURAL_OPTIMIZATION_RELEASE TRUE)
        set_property(TARGET _InfernuxBootstrap PROPERTY INTERPROCEDURAL_OPTIMIZATION_RELEASE TRUE)
        set_property(TARGET InfernuxRuntime PROPERTY INTERPROCEDURAL_OPTIMIZATION_RELEASE TRUE)
        if(WIN32)
            # Auto-exported subsystem DLLs cannot consume MSVC /GL objects.
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

# SPIRV-Cross is compiled for cross targets and supplied by the desktop Vulkan
# SDK or system packages for native builds.
if(INFERNUX_SPIRV_CROSS_TARGETS)
    target_link_libraries(InfernuxShaderCompiler PRIVATE
        ${INFERNUX_SPIRV_CROSS_TARGETS}
    )
    target_compile_definitions(InfernuxShaderCompiler PRIVATE
        INFERNUX_SPIRV_CROSS_FLAT_INCLUDE=1
    )
elseif(DEFINED ENV{VULKAN_SDK})
    set(VULKAN_SDK_PATH $ENV{VULKAN_SDK})
    target_include_directories(InfernuxRuntime PRIVATE ${VULKAN_SDK_PATH}/Include)

    if(WIN32)
        target_link_libraries(InfernuxShaderCompiler PRIVATE
            ${VULKAN_SDK_PATH}/Lib/spirv-cross-core.lib
            ${VULKAN_SDK_PATH}/Lib/spirv-cross-glsl.lib
            ${VULKAN_SDK_PATH}/Lib/spirv-cross-cpp.lib
            ${VULKAN_SDK_PATH}/Lib/spirv-cross-reflect.lib
        )
    else()
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
        message(FATAL_ERROR
            "SPIRV-Cross libraries are required by InfernuxShaderCompiler. "
            "Install spirv-cross-dev or set VULKAN_SDK to a complete SDK.")
    endif()
endif()

foreach(_infernux_target ${INFERNUX_NATIVE_TARGETS})
    target_compile_definitions(${_infernux_target} PRIVATE
        GLM_FORCE_DEPTH_ZERO_TO_ONE
        GLM_FORCE_LEFT_HANDED
        $<$<CONFIG:RelWithDebInfo>:INFERNUX_FRAME_PROFILE=1>
        $<$<CONFIG:Debug>:INFERNUX_FILE_LOGGING=1>
        $<$<CONFIG:RelWithDebInfo>:INFERNUX_FILE_LOGGING=1>
        $<$<CONFIG:Release>:INFERNUX_FILE_LOGGING=1>
        $<$<CONFIG:MinSizeRel>:INFERNUX_FILE_LOGGING=1>
        $<$<CONFIG:Release>:INFERNUX_DEFERRED_FILE_LOGGING=1>
        $<$<CONFIG:MinSizeRel>:INFERNUX_DEFERRED_FILE_LOGGING=1>
        $<$<OR:$<CONFIG:Release>,$<CONFIG:MinSizeRel>>:INFERNUX_COMPILE_OUT_DEBUG_LOGS=1>
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
    ${_infernux_vulkan_loader}
    Jolt
    GPUOpen::VulkanMemoryAllocator
)
target_link_libraries(InfernuxFoundation PUBLIC
    glm::glm
    stb
    ${_infernux_vulkan_loader}
)
target_link_libraries(InfernuxRenderCore PUBLIC glm::glm)
target_link_libraries(InfernuxRendererRuntime PUBLIC glm::glm)
target_link_libraries(InfernuxParticleRuntime PUBLIC glm::glm)
target_link_libraries(InfernuxShaderCompiler PUBLIC ${_infernux_vulkan_loader})
target_link_libraries(InfernuxVulkanBackend PUBLIC
    SDL3::SDL3
    glm::glm
    ${_infernux_vulkan_loader}
    GPUOpen::VulkanMemoryAllocator
)

find_package(ZLIB QUIET)
if(TARGET zlibstatic)
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
        "set INFERNUX_ZSTD_SOURCE_DIR or enable INFERNUX_PLAYER_PACK_FETCH_ZSTD")
endif()
target_link_libraries(InfernuxFoundation PRIVATE ${INFERNUX_ZSTD_TARGET})
