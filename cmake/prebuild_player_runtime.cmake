if(NOT INFERNUX_BUILD_CONFIG STREQUAL "Release")
    message(STATUS "Skipping Player Runtime Pack for ${INFERNUX_BUILD_CONFIG} configuration")
    return()
endif()

foreach(_required IN ITEMS INFERNUX_SOURCE_DIR PYTHON_EXECUTABLE NATIVE_MODULE_DIR OUTPUT_ROOT)
    if(NOT DEFINED ${_required} OR "${${_required}}" STREQUAL "")
        message(FATAL_ERROR "prebuild_player_runtime.cmake requires ${_required}")
    endif()
endforeach()

message(STATUS "Prebuilding LTO Release Player Runtime Pack and optional parallel build cache")
execute_process(
    COMMAND ${CMAKE_COMMAND} -E env
        "PYTHONPATH=${INFERNUX_SOURCE_DIR}/python"
        "INFERNUX_NATIVE_MODULE_DIR=${NATIVE_MODULE_DIR}"
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
