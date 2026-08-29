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
        COMMENT "Stage ${_infernux_dll} shared library in the build tree"
        VERBATIM
    )
endforeach()

add_custom_command(TARGET _Infernux POST_BUILD
    # Remove native modules from other Python ABIs/builds first so wheels can
    # never ship two _Infernux modules (GitHub issue #47).
    COMMAND ${CMAKE_COMMAND}
        "-DSOURCE=$<TARGET_FILE:_Infernux>"
        "-DTARGET_DIR=$<TARGET_FILE_DIR:_Infernux>"
        "-DKEEP_NAME=$<TARGET_FILE_NAME:_Infernux>"
        -P "${CMAKE_SOURCE_DIR}/cmake/stage_native_module.cmake"
    COMMAND ${CMAKE_COMMAND}
        "-DSOURCE=$<TARGET_FILE:_Infernux>"
        "-DTARGET_DIR=${PYTHON_TARGET_DIR}"
        "-DKEEP_NAME=$<TARGET_FILE_NAME:_Infernux>"
        -P "${CMAKE_SOURCE_DIR}/cmake/stage_native_module.cmake"

    COMMENT "Stage _Infernux binding module in the build tree"
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
        COMMENT "Stage dependent dylibs in the build tree"
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
        COMMENT "Stage dependent shared libraries in the build tree"
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
# This is an explicit stage in the package_python dependency chain.
# The script exits immediately for non-Release configurations (for example
# RelWithDebInfo), so debug installs still package without building a Player
# Runtime Pack.
set(INFERNUX_PREBUILT_RUNTIME_DIR "${CMAKE_BINARY_DIR}/runtime-packs")
set(INFERNUX_PREBUILT_RUNTIME_MODULE_DIR "${CMAKE_BINARY_DIR}/runtime-modules")
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
        "-DPLAYER_HOST_PATH=${INFERNUX_PLAYER_HOST_BUILD_PATH}"
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
        "-DTARGET_DIR=$<TARGET_FILE_DIR:_InfernuxBootstrap>"
        "-DKEEP_NAME=$<TARGET_FILE_NAME:_InfernuxBootstrap>"
        -P "${CMAKE_SOURCE_DIR}/cmake/stage_native_module.cmake"
    COMMAND ${CMAKE_COMMAND}
        "-DSOURCE=$<TARGET_FILE:_InfernuxBootstrap>"
        "-DTARGET_DIR=${PYTHON_TARGET_DIR}"
        "-DKEEP_NAME=$<TARGET_FILE_NAME:_InfernuxBootstrap>"
        -P "${CMAKE_SOURCE_DIR}/cmake/stage_native_module.cmake"
    COMMENT "Stage _InfernuxBootstrap binding module in the build tree"
    VERBATIM
)

# ------------------------------------------------------------------------------
# Install components used as deterministic wheel staging inputs
# ------------------------------------------------------------------------------
set(INFERNUX_PYTHON_INSTALL_COMPONENT "PythonWheel")

if(NOT INFERNUX_OFFICIAL_PLUGIN_OUTPUT_DIR)
    message(FATAL_ERROR
        "INFERNUX_OFFICIAL_PLUGIN_OUTPUT_DIR must be defined before install rules are declared")
endif()

install(
    DIRECTORY "${CMAKE_SOURCE_DIR}/python/Infernux/"
    DESTINATION "python/Infernux"
    COMPONENT ${INFERNUX_PYTHON_INSTALL_COMPONENT}
    PATTERN "__pycache__" EXCLUDE
    PATTERN "*.pyc" EXCLUDE
    PATTERN "*.pyo" EXCLUDE
    PATTERN "*.dll" EXCLUDE
    PATTERN "*.pyd" EXCLUDE
    PATTERN "*.so" EXCLUDE
    PATTERN "*.dylib" EXCLUDE
    PATTERN "*.meta" EXCLUDE
    PATTERN "_runtime_packs" EXCLUDE
    PATTERN "_runtime_modules" EXCLUDE
    PATTERN "official_packages" EXCLUDE
    PATTERN "player_runtime" EXCLUDE
)

install(
    FILES
        "${CMAKE_SOURCE_DIR}/pyproject.toml"
        "${CMAKE_SOURCE_DIR}/setup.py"
        "${CMAKE_SOURCE_DIR}/MANIFEST.in"
        "${CMAKE_SOURCE_DIR}/README.md"
        "${CMAKE_SOURCE_DIR}/README-zh.md"
        "${CMAKE_SOURCE_DIR}/LICENSE"
    DESTINATION "."
    COMPONENT ${INFERNUX_PYTHON_INSTALL_COMPONENT}
)

install(
    TARGETS _Infernux _InfernuxBootstrap ${INFERNUX_RUNTIME_DLL_TARGETS}
    RUNTIME DESTINATION "python/Infernux/lib"
        COMPONENT ${INFERNUX_PYTHON_INSTALL_COMPONENT}
    LIBRARY DESTINATION "python/Infernux/lib"
        COMPONENT ${INFERNUX_PYTHON_INSTALL_COMPONENT}
)

install(
    TARGETS assimp SDL3-shared Jolt
    RUNTIME DESTINATION "python/Infernux/lib"
        COMPONENT ${INFERNUX_PYTHON_INSTALL_COMPONENT}
    LIBRARY DESTINATION "python/Infernux/lib"
        COMPONENT ${INFERNUX_PYTHON_INSTALL_COMPONENT}
        NAMELINK_COMPONENT ${INFERNUX_PYTHON_INSTALL_COMPONENT}
)
install(
    FILES ${CMAKE_INSTALL_SYSTEM_RUNTIME_LIBS}
    DESTINATION "python/Infernux/lib"
    COMPONENT ${INFERNUX_PYTHON_INSTALL_COMPONENT}
)

install(
    DIRECTORY "${INFERNUX_PREBUILT_RUNTIME_DIR}/"
    DESTINATION "python/Infernux/_runtime_packs"
    COMPONENT ${INFERNUX_PYTHON_INSTALL_COMPONENT}
    OPTIONAL
)
install(
    DIRECTORY "${INFERNUX_PREBUILT_RUNTIME_MODULE_DIR}/"
    DESTINATION "python/Infernux/_runtime_modules"
    COMPONENT ${INFERNUX_PYTHON_INSTALL_COMPONENT}
    OPTIONAL
)
install(
    DIRECTORY "${INFERNUX_OFFICIAL_PLUGIN_OUTPUT_DIR}/"
    DESTINATION "python/Infernux/resources/official_packages"
    COMPONENT ${INFERNUX_PYTHON_INSTALL_COMPONENT}
    OPTIONAL
)

if(TARGET InfernuxPlayerHost)
    install(
        TARGETS InfernuxPlayerHost
        RUNTIME DESTINATION "python/Infernux/resources/player_runtime"
            COMPONENT ${INFERNUX_PYTHON_INSTALL_COMPONENT}
    )
endif()
