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

if(INFERNUX_RUNTIME_STATIC)
    # A shipping build has no composition DLL. Remove stale shared-test or
    # pre-refactor copies before dependency scans and wheel staging.
    add_custom_command(TARGET _Infernux POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E rm -f
            "${PYTHON_TARGET_DIR}/InfernuxRuntime.dll"
            "$<TARGET_FILE_DIR:_Infernux>/InfernuxRuntime.dll"
        VERBATIM
    )
endif()

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
    COMMAND ${CMAKE_COMMAND} -E rm -f
        "${PYTHON_TARGET_DIR}/InfernuxRuntime.dll"
        "${PYTHON_TARGET_DIR}/SPIRV.dll"
        "${PYTHON_TARGET_DIR}/SPVRemapper.dll"
        "${PYTHON_TARGET_DIR}/glslang-default-resource-limits.dll"
        "${PYTHON_TARGET_DIR}/glslang.dll"
        "$<TARGET_FILE_DIR:_Infernux>/InfernuxRuntime.dll"
        "$<TARGET_FILE_DIR:_Infernux>/SPIRV.dll"
        "$<TARGET_FILE_DIR:_Infernux>/SPVRemapper.dll"
        "$<TARGET_FILE_DIR:_Infernux>/glslang-default-resource-limits.dll"
        "$<TARGET_FILE_DIR:_Infernux>/glslang.dll"
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

add_custom_command(TARGET _InfernuxBootstrap POST_BUILD
    COMMAND ${CMAKE_COMMAND}
        "-DSOURCE=$<TARGET_FILE:_InfernuxBootstrap>"
        "-DTARGET_DIR=${PYTHON_TARGET_DIR}"
        "-DKEEP_NAME=$<TARGET_FILE_NAME:_InfernuxBootstrap>"
        -P "${CMAKE_SOURCE_DIR}/cmake/stage_native_module.cmake"
    COMMENT "Copy _InfernuxBootstrap binding module to python/Infernux/lib"
    VERBATIM
)

