# ------------------------------------------------------------------------------
# Python Packaging and Installation
# ------------------------------------------------------------------------------
add_subdirectory(external/plugins)

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
add_dependencies(
    package_and_install_python
    prebuild_player_runtime
    infernux_official_plugins
)
