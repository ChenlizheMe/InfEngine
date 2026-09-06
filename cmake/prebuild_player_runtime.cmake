foreach(_required IN ITEMS
    INFERNUX_SOURCE_DIR
    PYTHON_EXECUTABLE
    NATIVE_MODULE_DIR
    OUTPUT_ROOT
    MODULE_OUTPUT_ROOT
    PLATFORM_PLAYER_OUTPUT
    BUILD_CACHE_ROOT
)
    if(NOT DEFINED ${_required} OR "${${_required}}" STREQUAL "")
        message(FATAL_ERROR "prebuild_player_runtime.cmake requires ${_required}")
    endif()
endforeach()

if(NOT INFERNUX_BUILD_CONFIG STREQUAL "Release")
    message(FATAL_ERROR "Platform Player publication requires a Release configuration")
endif()

# The generated archive staging is separate from both source assets and the
# persistent compiler cache. The final payload is published by this target
# directly into the platform subrepository, not into the Editor wheel.
file(REMOVE_RECURSE "${OUTPUT_ROOT}" "${MODULE_OUTPUT_ROOT}")

message(STATUS "Prebuilding LTO Release Player Runtime Pack and optional parallel build cache")
execute_process(
    COMMAND ${CMAKE_COMMAND} -E env
        "PYTHONPATH=${INFERNUX_SOURCE_DIR}/python"
        "PYTHONDONTWRITEBYTECODE=1"
        "INFERNUX_NATIVE_MODULE_DIR=${NATIVE_MODULE_DIR}"
        "INFERNUX_PLAYER_HOST_PATH=${PLAYER_HOST_PATH}"
        "${PYTHON_EXECUTABLE}" -m Infernux.engine.prebuilt_runtime
        --profile release
        --output-root "${OUTPUT_ROOT}"
        --build-cache-root "${BUILD_CACHE_ROOT}"
        --platform-player-output "${PLATFORM_PLAYER_OUTPUT}"
    WORKING_DIRECTORY "${INFERNUX_SOURCE_DIR}"
    COMMAND_ECHO STDOUT
    RESULT_VARIABLE _runtime_pack_result
)

if(NOT _runtime_pack_result EQUAL 0)
    message(FATAL_ERROR "Player Runtime Pack prebuild failed with exit code ${_runtime_pack_result}")
endif()
