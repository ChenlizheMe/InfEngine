if(NOT DEFINED PYTHON_DIR)
    message(FATAL_ERROR "PYTHON_DIR is not defined")
endif()

if(DEFINED INFERNUX_BUILD_CONFIG AND NOT INFERNUX_BUILD_CONFIG STREQUAL "Release")
    message(STATUS "Skipping Python artifact cleanup for ${INFERNUX_BUILD_CONFIG} configuration")
    return()
endif()

if(NOT EXISTS "${PYTHON_DIR}")
    message(STATUS "Python directory not found, skipping artifact cleanup: ${PYTHON_DIR}")
    return()
endif()

file(GLOB_RECURSE _python_entries LIST_DIRECTORIES true "${PYTHON_DIR}/*")

set(_removed_pycache_count 0)
foreach(_entry IN LISTS _python_entries)
    if(IS_DIRECTORY "${_entry}")
        get_filename_component(_entry_name "${_entry}" NAME)
        if(_entry_name STREQUAL "__pycache__")
            file(REMOVE_RECURSE "${_entry}")
            math(EXPR _removed_pycache_count "${_removed_pycache_count} + 1")
            message(STATUS "Removed __pycache__: ${_entry}")
        endif()
    endif()
endforeach()

message(STATUS
    "Python artifact cleanup complete. Removed ${_removed_pycache_count} __pycache__ directories."
)
