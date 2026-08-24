include_guard(GLOBAL)

if(NOT Python3_EXECUTABLE)
    message(FATAL_ERROR "InfernuxHub.cmake requires Python3_EXECUTABLE")
endif()

add_custom_target(prepare_bundled_python_runtime
    COMMAND ${CMAKE_COMMAND} -E echo "Ensuring bundled Python 3.12 runtime is available..."
    COMMAND ${Python3_EXECUTABLE}
        "${CMAKE_SOURCE_DIR}/packaging/stage_bundled_python_runtime.py"
        --dest-root "${CMAKE_SOURCE_DIR}/out/package/runtime/python312"
    WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
    COMMENT "Stage bundled full Python runtime assets"
)

add_custom_target(infernux_hub
    COMMAND ${CMAKE_COMMAND} -E echo "Compiling Infernux Hub with the MSBuild/MSVC release toolchain..."
    COMMAND ${Python3_EXECUTABLE}
        "${CMAKE_SOURCE_DIR}/packaging/build_hub.py"
        --target hub
        --source-root "${CMAKE_SOURCE_DIR}"
        --build-dir "${CMAKE_BINARY_DIR}/hub_build"
        --package-dir "${CMAKE_SOURCE_DIR}/out/package"
        --cmake-generator "${CMAKE_GENERATOR}"
    WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}/packaging"
    DEPENDS prepare_bundled_python_runtime
    COMMENT "Build MSVC-compiled Infernux Hub via MSBuild → out/package/hub/"
)

add_custom_target(infernux_hub_installer
    COMMAND ${CMAKE_COMMAND} -E echo "Building graphical Infernux Hub installer with MSBuild/MSVC..."
    COMMAND ${Python3_EXECUTABLE}
        "${CMAKE_SOURCE_DIR}/packaging/build_hub.py"
        --target installer
        --source-root "${CMAKE_SOURCE_DIR}"
        --build-dir "${CMAKE_BINARY_DIR}/installer_build"
        --package-dir "${CMAKE_SOURCE_DIR}/out/package"
        --cmake-generator "${CMAKE_GENERATOR}"
    WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}/packaging"
    DEPENDS infernux_hub
    COMMENT "Build MSVC graphical Infernux Hub installer via MSBuild → out/package/installer/"
)
