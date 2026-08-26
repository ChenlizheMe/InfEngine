set(INFERNUX_OFFICIAL_PLUGIN_OUTPUT_DIR
    "${CMAKE_SOURCE_DIR}/python/Infernux/resources/official_packages"
)

file(GLOB_RECURSE INFERNUX_OFFICIAL_PLUGIN_SOURCES CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/infernux_mcp/*"
)
list(FILTER INFERNUX_OFFICIAL_PLUGIN_SOURCES EXCLUDE REGEX "/\\.git($|/)")

add_custom_target(infernux_official_plugins
    COMMAND ${CMAKE_COMMAND} -E env
        "PYTHONPATH=${CMAKE_SOURCE_DIR}/python"
        "INFERNUX_NATIVE_MODULE_DIR=$<TARGET_FILE_DIR:_Infernux>"
        "${Python3_EXECUTABLE}"
        "${CMAKE_CURRENT_SOURCE_DIR}/build_official_plugins.py"
        --source-root "${CMAKE_CURRENT_SOURCE_DIR}"
        --output-root "${INFERNUX_OFFICIAL_PLUGIN_OUTPUT_DIR}"
        --catalog "${CMAKE_CURRENT_SOURCE_DIR}/plugins.json"
    DEPENDS
        _Infernux
        "${CMAKE_CURRENT_SOURCE_DIR}/build_official_plugins.py"
        "${CMAKE_CURRENT_SOURCE_DIR}/plugins.json"
        ${INFERNUX_OFFICIAL_PLUGIN_SOURCES}
    COMMENT "Building official Infernux plugins"
    VERBATIM
)
