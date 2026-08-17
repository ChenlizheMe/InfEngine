foreach(_required IN ITEMS SOURCE TARGET_DIR KEEP_NAME)
    if(NOT DEFINED ${_required} OR "${${_required}}" STREQUAL "")
        message(FATAL_ERROR "stage_native_module.cmake requires ${_required}")
    endif()
endforeach()

include("${CMAKE_CURRENT_LIST_DIR}/stable_copy.cmake")

file(TO_CMAKE_PATH "${TARGET_DIR}" _target_key)
string(TOLOWER "${_target_key}" _target_key)
string(MD5 _target_hash "${_target_key}")
if(DEFINED ENV{TEMP} AND NOT "$ENV{TEMP}" STREQUAL "")
    set(_lock_root "$ENV{TEMP}")
elseif(DEFINED ENV{TMPDIR} AND NOT "$ENV{TMPDIR}" STREQUAL "")
    set(_lock_root "$ENV{TMPDIR}")
else()
    set(_lock_root "${CMAKE_CURRENT_BINARY_DIR}")
endif()
file(LOCK "${_lock_root}/infernux-native-dir-${_target_hash}.lock"
    GUARD PROCESS TIMEOUT 120 RESULT_VARIABLE _lock_result)
if(NOT _lock_result STREQUAL "0")
    message(FATAL_ERROR "Timed out waiting for native package directory: ${_lock_result}")
endif()

string(REGEX REPLACE "\\..*$" "" _module_prefix "${KEEP_NAME}")
file(GLOB _stale_modules
    "${TARGET_DIR}/${_module_prefix}.*.pyd"
    "${TARGET_DIR}/${_module_prefix}.*.so"
    "${TARGET_DIR}/${_module_prefix}.pyd"
    "${TARGET_DIR}/${_module_prefix}.so"
)
foreach(_module IN LISTS _stale_modules)
    get_filename_component(_name "${_module}" NAME)
    if(NOT _name STREQUAL "${KEEP_NAME}")
        file(REMOVE "${_module}")
        if(EXISTS "${_module}")
            message(FATAL_ERROR
                "Cannot remove stale native module '${_module}'; close running Infernux processes")
        endif()
        message(STATUS "Removed stale native module: ${_name} (keeping ${KEEP_NAME})")
    endif()
endforeach()

infernux_copy_file_stable("${SOURCE}" "${TARGET_DIR}/${KEEP_NAME}")
