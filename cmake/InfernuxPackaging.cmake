# Python wheel staging, packaging, and optional environment installation.

set(INFERNUX_PYTHON_STAGE_DIR "${INFERNUX_STAGE_DIR}/python-wheel-source")
set(INFERNUX_PYTHON_WHEEL_DIR "${INFERNUX_STAGE_DIR}/wheels")

add_custom_target(stage_python_package
    COMMAND ${CMAKE_COMMAND} -E rm -rf "${INFERNUX_PYTHON_STAGE_DIR}"
    COMMAND ${CMAKE_COMMAND} --install "${CMAKE_BINARY_DIR}"
        --config "$<CONFIG>"
        --prefix "${INFERNUX_PYTHON_STAGE_DIR}"
        --component ${INFERNUX_PYTHON_INSTALL_COMPONENT}
    DEPENDS
        prebuild_player_runtime
        infernux_official_plugins
    COMMENT "Assembling the Python wheel source tree after native and official plugin builds"
    VERBATIM
)

add_custom_target(package_python
    COMMAND ${CMAKE_COMMAND} -E echo "Ensuring Python packaging tools are available..."
    COMMAND ${CMAKE_COMMAND}
        -DINFERNUX_SOURCE_DIR=${CMAKE_SOURCE_DIR}
        -DPYTHON_EXECUTABLE=${Python3_EXECUTABLE}
        -P "${CMAKE_SOURCE_DIR}/cmake/ensure_python_packaging_tools.cmake"

    COMMAND ${CMAKE_COMMAND} -E rm -rf "${INFERNUX_PYTHON_STAGE_DIR}/build"
    COMMAND ${CMAKE_COMMAND} -E rm -rf "${INFERNUX_PYTHON_STAGE_DIR}/python/Infernux.egg-info"
    COMMAND ${CMAKE_COMMAND} -E rm -rf "${INFERNUX_PYTHON_WHEEL_DIR}"
    COMMAND ${CMAKE_COMMAND} -E make_directory "${INFERNUX_PYTHON_WHEEL_DIR}"
    COMMAND ${CMAKE_COMMAND} -E chdir "${INFERNUX_PYTHON_STAGE_DIR}"
        ${CMAKE_COMMAND} -E env
        "INFERNUX_SOURCE_DIR=${INFERNUX_PYTHON_STAGE_DIR}"
        "INFERNUX_STAGED_WHEEL_BUILD=1"
        "${Python3_EXECUTABLE}" -m build --wheel --no-isolation
        --outdir "${INFERNUX_PYTHON_WHEEL_DIR}"

    COMMAND ${CMAKE_COMMAND}
        -DINFERNUX_SOURCE_DIR=${INFERNUX_PYTHON_STAGE_DIR}
        -DINFERNUX_WHEEL_DIR=${INFERNUX_PYTHON_WHEEL_DIR}
        -P "${CMAKE_SOURCE_DIR}/cmake/verify_python_wheel.cmake"

    COMMAND ${CMAKE_COMMAND} -E copy_directory
        "${INFERNUX_PYTHON_WHEEL_DIR}" "${INFERNUX_RELEASE_DIR}"

    DEPENDS stage_python_package
    COMMENT "Building and verifying the Infernux Python wheel"
    VERBATIM
)

add_custom_target(install_python_wheel
    COMMAND ${CMAKE_COMMAND}
        -DINFERNUX_SOURCE_DIR=${CMAKE_SOURCE_DIR}
        -DINFERNUX_WHEEL_DIR=${INFERNUX_PYTHON_WHEEL_DIR}
        -DPYTHON_EXECUTABLE=${Python3_EXECUTABLE}
        -P "${CMAKE_SOURCE_DIR}/install_wheel.cmake"
    DEPENDS package_python
    COMMENT "Installing the verified Infernux wheel into the active Python environment"
    VERBATIM
)
