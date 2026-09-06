# Only wheel-bundled archives are generated in the build tree. The default MCP
# package is copied into the source-checkout resources used by the editor; the
# platform packages remain release artifacts and are downloaded on demand.
# Wheel staging depends on this target, preserving the order native module →
# official plugins → wheel.
set(INFERNUX_OFFICIAL_PLUGIN_OUTPUT_DIR
    "${CMAKE_BINARY_DIR}/official-plugins"
)
set(INFERNUX_OFFICIAL_PLUGIN_OUTPUT_DIR
    "${INFERNUX_OFFICIAL_PLUGIN_OUTPUT_DIR}"
    PARENT_SCOPE
)

# This phony target rebuilds the packages on every invocation.  Do not attach a
# recursive CONFIGURE_DEPENDS glob here: generated Python cache files would
# otherwise force an unrelated whole-project CMake reconfigure even though the
# package exporter explicitly excludes them.

set(INFERNUX_OFFICIAL_PLUGIN_ARTIFACTS
    "${INFERNUX_OFFICIAL_PLUGIN_OUTPUT_DIR}/official-registry.json"
    "${INFERNUX_OFFICIAL_PLUGIN_OUTPUT_DIR}/default-libraries.json"
    "${INFERNUX_OFFICIAL_PLUGIN_OUTPUT_DIR}/infernux.mcp.inxpkg"
    "${CMAKE_SOURCE_DIR}/python/Infernux/resources/infernux.mcp.inxpkg"
)

add_custom_target(infernux_official_plugins ALL
    COMMAND ${CMAKE_COMMAND} -E env
        "PYTHONPATH=${CMAKE_SOURCE_DIR}/python"
        "INFERNUX_NATIVE_MODULE_DIR=$<TARGET_FILE_DIR:_Infernux>"
        "${Python3_EXECUTABLE}"
        "${CMAKE_CURRENT_SOURCE_DIR}/build_official_plugins.py"
        --source-root "${CMAKE_CURRENT_SOURCE_DIR}"
        --output-root "${INFERNUX_OFFICIAL_PLUGIN_OUTPUT_DIR}"
        --catalog "${CMAKE_CURRENT_SOURCE_DIR}/plugins.json"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${INFERNUX_OFFICIAL_PLUGIN_OUTPUT_DIR}/infernux.mcp.inxpkg"
        "${CMAKE_SOURCE_DIR}/python/Infernux/resources/infernux.mcp.inxpkg"
    DEPENDS
        _Infernux
        "${CMAKE_CURRENT_SOURCE_DIR}/build_official_plugins.py"
        "${CMAKE_CURRENT_SOURCE_DIR}/plugins.json"
    BYPRODUCTS ${INFERNUX_OFFICIAL_PLUGIN_ARTIFACTS}
    COMMENT "Building official Infernux plugins"
    VERBATIM
)

# Platform plugin releases are built and published by each plugin repository.
# The engine owns only catalog generation and its bundled default packages.
