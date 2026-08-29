set(INFERNUX_OFFICIAL_PLUGIN_OUTPUT_DIR
    "${CMAKE_BINARY_DIR}/official-packages"
)
set(INFERNUX_OFFICIAL_PLUGIN_OUTPUT_DIR
    "${INFERNUX_OFFICIAL_PLUGIN_OUTPUT_DIR}"
    PARENT_SCOPE
)

file(GLOB_RECURSE INFERNUX_OFFICIAL_PLUGIN_SOURCES CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/infernux_*/*"
)
list(FILTER INFERNUX_OFFICIAL_PLUGIN_SOURCES EXCLUDE REGEX "/(\\.git|__pycache__)($|/)")
list(FILTER INFERNUX_OFFICIAL_PLUGIN_SOURCES EXCLUDE REGEX "\\.py[co]$")

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
