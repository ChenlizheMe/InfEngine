if(NOT DEFINED INFERNUX_SOURCE_DIR OR INFERNUX_SOURCE_DIR STREQUAL "")
    message(FATAL_ERROR "INFERNUX_SOURCE_DIR is required")
endif()
if(NOT DEFINED PYTHON_EXECUTABLE OR PYTHON_EXECUTABLE STREQUAL "")
    message(FATAL_ERROR "PYTHON_EXECUTABLE is required")
endif()
if(NOT DEFINED INFERNUX_PLAYER_OUTPUT_DIR OR INFERNUX_PLAYER_OUTPUT_DIR STREQUAL "")
    message(FATAL_ERROR
        "INFERNUX_PLAYER_OUTPUT_DIR is required; build a Player first and pass its output directory"
    )
endif()

execute_process(
    COMMAND ${CMAKE_COMMAND} -E env
        "PYTHONPATH=${INFERNUX_SOURCE_DIR}/python"
        "${PYTHON_EXECUTABLE}" -m Infernux.engine.player_package_audit
        --root "${INFERNUX_PLAYER_OUTPUT_DIR}"
    WORKING_DIRECTORY "${INFERNUX_SOURCE_DIR}"
    COMMAND_ECHO STDOUT
    RESULT_VARIABLE _audit_result
)
if(NOT _audit_result EQUAL 0)
    message(FATAL_ERROR "Player package audit failed with exit code ${_audit_result}")
endif()
