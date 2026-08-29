if(NOT DEFINED INFERNUX_WEB_HTML OR NOT EXISTS "${INFERNUX_WEB_HTML}")
    message(FATAL_ERROR "The linked Web Player HTML file is missing")
endif()
string(LENGTH "${INFERNUX_WEB_ASSET_REVISION}" _infernux_web_revision_length)
if(NOT INFERNUX_WEB_ASSET_REVISION MATCHES "^[0-9a-f]+$" OR
   NOT _infernux_web_revision_length EQUAL 24)
    message(FATAL_ERROR "The Web Player asset revision is invalid")
endif()

file(READ "${INFERNUX_WEB_HTML}" _infernux_web_html)
get_filename_component(_infernux_web_output_dir "${INFERNUX_WEB_HTML}" DIRECTORY)
foreach(_infernux_web_extension js wasm data)
    set(_infernux_web_stable
        "${_infernux_web_output_dir}/infernux-player.${_infernux_web_extension}")
    set(_infernux_web_versioned
        "${_infernux_web_output_dir}/infernux-player.${INFERNUX_WEB_ASSET_REVISION}.${_infernux_web_extension}")
    if(NOT EXISTS "${_infernux_web_stable}")
        message(FATAL_ERROR
            "The linked Web Player ${_infernux_web_extension} file is missing")
    endif()
    execute_process(
        COMMAND "${CMAKE_COMMAND}" -E copy_if_different
            "${_infernux_web_stable}" "${_infernux_web_versioned}"
        COMMAND_ERROR_IS_FATAL ANY
    )
endforeach()

set(_infernux_web_script "infernux-player.js")
set(_infernux_web_versioned_script
    "infernux-player.${INFERNUX_WEB_ASSET_REVISION}.js")
string(FIND "${_infernux_web_html}" "${_infernux_web_versioned_script}"
    _infernux_web_already_stamped)
if(_infernux_web_already_stamped EQUAL -1)
    string(FIND "${_infernux_web_html}" "${_infernux_web_script}"
        _infernux_web_unstamped)
    if(_infernux_web_unstamped EQUAL -1)
        message(FATAL_ERROR "The linked Web Player HTML has no JavaScript entry point")
    endif()
    string(REPLACE "${_infernux_web_script}" "${_infernux_web_versioned_script}"
        _infernux_web_html "${_infernux_web_html}")
    file(WRITE "${INFERNUX_WEB_HTML}" "${_infernux_web_html}")
endif()
