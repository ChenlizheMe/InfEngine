# Shared ownership for disposable assembly trees and versioned release files.
include_guard(GLOBAL)

get_filename_component(_infernux_build_name "${CMAKE_BINARY_DIR}" NAME)
set(INFERNUX_STAGE_DIR "${CMAKE_SOURCE_DIR}/out/stage/${_infernux_build_name}")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
    "${CMAKE_SOURCE_DIR}/pyproject.toml")
execute_process(
    COMMAND "${Python3_EXECUTABLE}" -c
        "import pathlib,sys,tomllib; print(tomllib.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))['project']['version'])"
        "${CMAKE_SOURCE_DIR}/pyproject.toml"
    OUTPUT_VARIABLE INFERNUX_PACKAGE_VERSION
    OUTPUT_STRIP_TRAILING_WHITESPACE
    COMMAND_ERROR_IS_FATAL ANY
)
set(INFERNUX_RELEASE_DIR "${CMAKE_SOURCE_DIR}/dist/releases/${INFERNUX_PACKAGE_VERSION}")
