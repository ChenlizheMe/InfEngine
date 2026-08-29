foreach(_required IN ITEMS
    INFERNUX_SOURCE_DIR
    PYTHON_EXECUTABLE
    NATIVE_MODULE_DIR
    OUTPUT_ROOT
    MODULE_OUTPUT_ROOT
)
    if(NOT DEFINED ${_required} OR "${${_required}}" STREQUAL "")
        message(FATAL_ERROR "prebuild_player_runtime.cmake requires ${_required}")
    endif()
endforeach()

# These directories are wheel staging outputs, not source assets or persistent
# Nuitka caches. Always reset them before deciding whether this configuration
# produces a Runtime Pack so stale hash directories, interrupted *.tmp trees,
# and removed ZIP-era payloads cannot leak into a later wheel.
file(REMOVE_RECURSE "${OUTPUT_ROOT}" "${MODULE_OUTPUT_ROOT}")

if(NOT INFERNUX_BUILD_CONFIG STREQUAL "Release")
    message(STATUS
        "Cleared wheel Runtime Pack staging; ${INFERNUX_BUILD_CONFIG} does not ship prebuilt runtime payloads"
    )
    return()
endif()

message(STATUS "Prebuilding LTO Release Player Runtime Pack and optional parallel build cache")
execute_process(
    COMMAND ${CMAKE_COMMAND} -E env
        "PYTHONPATH=${INFERNUX_SOURCE_DIR}/python"
        "INFERNUX_NATIVE_MODULE_DIR=${NATIVE_MODULE_DIR}"
        "INFERNUX_PLAYER_HOST_PATH=${PLAYER_HOST_PATH}"
        "${PYTHON_EXECUTABLE}" -m Infernux.engine.prebuilt_runtime
        --profile release
        --output-root "${OUTPUT_ROOT}"
    WORKING_DIRECTORY "${INFERNUX_SOURCE_DIR}"
    COMMAND_ECHO STDOUT
    RESULT_VARIABLE _runtime_pack_result
)

if(NOT _runtime_pack_result EQUAL 0)
    message(FATAL_ERROR "Player Runtime Pack prebuild failed with exit code ${_runtime_pack_result}")
endif()
