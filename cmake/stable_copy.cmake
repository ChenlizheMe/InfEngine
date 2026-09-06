# Stable file staging shared by native post-build and dependency collection.

function(infernux_copy_file_stable source destination)
    if(NOT EXISTS "${source}")
        message(FATAL_ERROR "Cannot stage missing build artifact: ${source}")
    endif()

    file(TO_CMAKE_PATH "${source}" _source_normalized)
    file(TO_CMAKE_PATH "${destination}" _destination_normalized)
    if(_source_normalized STREQUAL _destination_normalized)
        return()
    endif()

    get_filename_component(_destination_dir "${destination}" DIRECTORY)
    file(MAKE_DIRECTORY "${_destination_dir}")

    # Separate build trees may stage the same package file. Serialize by the
    # normalized destination path without leaving lock files in the wheel.
    file(TO_CMAKE_PATH "${destination}" _lock_key)
    string(TOLOWER "${_lock_key}" _lock_key)
    string(MD5 _lock_hash "${_lock_key}")
    if(DEFINED ENV{TEMP} AND NOT "$ENV{TEMP}" STREQUAL "")
        set(_lock_root "$ENV{TEMP}")
    elseif(DEFINED ENV{TMPDIR} AND NOT "$ENV{TMPDIR}" STREQUAL "")
        set(_lock_root "$ENV{TMPDIR}")
    else()
        set(_lock_root "${CMAKE_CURRENT_BINARY_DIR}")
    endif()
    file(LOCK "${_lock_root}/infernux-stage-${_lock_hash}.lock"
        GUARD FUNCTION TIMEOUT 120 RESULT_VARIABLE _lock_result)
    if(NOT _lock_result STREQUAL "0")
        message(FATAL_ERROR "Timed out waiting to stage ${destination}: ${_lock_result}")
    endif()

    set(_copy_result "")
    foreach(_attempt RANGE 1 5)
        file(COPY_FILE "${source}" "${destination}"
            ONLY_IF_DIFFERENT RESULT _copy_result)
        if(_copy_result STREQUAL "0")
            return()
        endif()
        execute_process(COMMAND "${CMAKE_COMMAND}" -E sleep 0.25)
    endforeach()
    message(FATAL_ERROR
        "Failed to stage '${source}' as '${destination}' after 5 attempts: ${_copy_result}")
endfunction()
