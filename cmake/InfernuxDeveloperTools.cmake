# Developer-only maintenance targets. Product targets and package assembly do
# not belong in this module.

find_program(INFERNUX_CLANG_FORMAT_EXECUTABLE
    NAMES clang-format clang-format-22 clang-format-21 clang-format-20
          clang-format-19 clang-format-18
)

if(INFERNUX_CLANG_FORMAT_EXECUTABLE)
    add_custom_target(clang_format
        COMMAND "${Python3_EXECUTABLE}"
            "${CMAKE_SOURCE_DIR}/cmake/run_clang_format.py"
            --clang-format "${INFERNUX_CLANG_FORMAT_EXECUTABLE}"
            --source-root "${CMAKE_SOURCE_DIR}/cpp"
        DEPENDS "${CMAKE_SOURCE_DIR}/cmake/run_clang_format.py"
        COMMENT "Running clang-format on source files"
        VERBATIM
    )
else()
    add_custom_target(clang_format
        COMMAND "${CMAKE_COMMAND}" -E echo
            "clang-format 18 or newer is required for the clang_format target"
        COMMAND "${CMAKE_COMMAND}" -E false
        VERBATIM
    )
endif()

add_custom_target(generate_api_docs
    COMMAND ${CMAKE_COMMAND} -E echo "Generating API reference docs..."
    COMMAND ${CMAKE_COMMAND} -E env PYTHONIOENCODING=utf-8
            python "${CMAKE_SOURCE_DIR}/docs/wiki/generate_api_docs.py"
    COMMENT "Auto-generate API reference Markdown from Python stubs"
)

add_custom_target(build_wiki_html
    COMMAND ${CMAKE_COMMAND} -E echo "Building MkDocs wiki..."
    COMMAND ${CMAKE_COMMAND} -E env PYTHONIOENCODING=utf-8
            python -m mkdocs build --clean -f "${CMAKE_SOURCE_DIR}/docs/wiki/mkdocs.yml"
    COMMENT "Generate wiki HTML into docs/wiki/site"
)
add_dependencies(build_wiki_html generate_api_docs)
