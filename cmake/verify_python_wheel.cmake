if(NOT DEFINED INFERNUX_SOURCE_DIR OR INFERNUX_SOURCE_DIR STREQUAL "")
    message(FATAL_ERROR "INFERNUX_SOURCE_DIR is required")
endif()

if(NOT DEFINED INFERNUX_WHEEL_DIR OR INFERNUX_WHEEL_DIR STREQUAL "")
    message(FATAL_ERROR "INFERNUX_WHEEL_DIR is required")
endif()

file(GLOB _wheels "${INFERNUX_WHEEL_DIR}/*.whl")
list(LENGTH _wheels _wheel_count)
if(NOT _wheel_count EQUAL 1)
    message(FATAL_ERROR "Expected exactly one wheel in ${INFERNUX_WHEEL_DIR}, found ${_wheel_count}")
endif()
list(GET _wheels 0 _wheel)

set(_package_root "${INFERNUX_SOURCE_DIR}/python")
set(_verify_root "${INFERNUX_WHEEL_DIR}/verify-native-payload")
file(REMOVE_RECURSE "${_verify_root}")
file(MAKE_DIRECTORY "${_verify_root}")
file(ARCHIVE_EXTRACT INPUT "${_wheel}" DESTINATION "${_verify_root}")

file(GLOB_RECURSE _forbidden_files LIST_DIRECTORIES false
    "${_verify_root}/*.bak"
    "${_verify_root}/*.exp"
    "${_verify_root}/*.lib"
    "${_verify_root}/*.meta"
    "${_verify_root}/*.pdb"
    "${_verify_root}/*.pyc"
    "${_verify_root}/*.pyo"
)
if(_forbidden_files)
    list(JOIN _forbidden_files "\n  " _forbidden_report)
    message(FATAL_ERROR
        "Wheel contains build-time or editor-only files:\n  ${_forbidden_report}"
    )
endif()

file(GLOB_RECURSE _unsupported_runtime_archives LIST_DIRECTORIES false
    "${_verify_root}/*.zip"
    "${_verify_root}/*.inxpack"
)
if(_unsupported_runtime_archives)
    list(JOIN _unsupported_runtime_archives "\n  " _unsupported_runtime_report)
    message(FATAL_ERROR
        "Wheel contains unsupported runtime containers; Runtime.inxrt/"
        "Parallel.inxmod is required:\n  ${_unsupported_runtime_report}"
    )
endif()

file(GLOB_RECURSE _native_runtime_archives LIST_DIRECTORIES false
    "${_verify_root}/*.inxrt"
    "${_verify_root}/*.inxpkg"
    "${_verify_root}/*.inxmod"
)
foreach(_runtime_archive IN LISTS _native_runtime_archives)
    file(READ "${_runtime_archive}" _runtime_magic OFFSET 0 LIMIT 8 HEX)
    string(TOLOWER "${_runtime_magic}" _runtime_magic)
    if(NOT _runtime_magic STREQUAL "494e58504b470000")
        message(FATAL_ERROR
            "Wheel contains a non-native runtime package: ${_runtime_archive}"
        )
    endif()
endforeach()

file(GLOB_RECURSE _native_files LIST_DIRECTORIES false
    "${_package_root}/Infernux/*.dll"
    "${_package_root}/Infernux/*.dylib"
    "${_package_root}/Infernux/*.pyd"
    "${_package_root}/Infernux/*.so"
)

if(NOT _native_files)
    message(FATAL_ERROR "No native package files found below ${_package_root}/Infernux")
endif()

file(GLOB _bootstrap_source_files
    "${_package_root}/Infernux/lib/_InfernuxBootstrap*.pyd"
    "${_package_root}/Infernux/lib/_InfernuxBootstrap*.so"
    "${_package_root}/Infernux/lib/_InfernuxBootstrap*.dylib"
)
if(NOT _bootstrap_source_files)
    message(FATAL_ERROR "Missing _InfernuxBootstrap native module in the package source tree")
endif()

foreach(_source_file IN LISTS _native_files)
    file(RELATIVE_PATH _relative_path "${_package_root}" "${_source_file}")
    set(_wheel_file "${_verify_root}/${_relative_path}")
    if(NOT EXISTS "${_wheel_file}")
        message(FATAL_ERROR "Wheel is missing native package file: ${_relative_path}")
    endif()

    execute_process(
        COMMAND "${CMAKE_COMMAND}" -E compare_files "${_source_file}" "${_wheel_file}"
        RESULT_VARIABLE _compare_result
    )
    if(NOT _compare_result EQUAL 0)
        message(FATAL_ERROR
            "Wheel contains a stale native package file: ${_relative_path}"
        )
    endif()
endforeach()

foreach(_bootstrap_source_file IN LISTS _bootstrap_source_files)
    file(RELATIVE_PATH _bootstrap_relative_path "${_package_root}" "${_bootstrap_source_file}")
    if(NOT EXISTS "${_verify_root}/${_bootstrap_relative_path}")
        message(FATAL_ERROR "Wheel is missing bootstrap native package file: ${_bootstrap_relative_path}")
    endif()
endforeach()

file(GLOB_RECURSE _bundled_plugins LIST_DIRECTORIES false
    "${_verify_root}/*.inxpkg"
)
list(LENGTH _bundled_plugins _bundled_plugin_count)
if(NOT _bundled_plugin_count EQUAL 1)
    message(FATAL_ERROR
        "Wheel must contain exactly the default MCP plugin, found "
        "${_bundled_plugin_count} InxPackages"
    )
endif()
list(GET _bundled_plugins 0 _bundled_plugin)
get_filename_component(_bundled_plugin_name "${_bundled_plugin}" NAME)
if(NOT _bundled_plugin_name STREQUAL "infernux.mcp.inxpkg")
    message(FATAL_ERROR
        "Wheel contains an unexpected built-in plugin: ${_bundled_plugin_name}"
    )
endif()

if(CMAKE_HOST_SYSTEM_NAME STREQUAL "Linux")
    find_program(_infernux_readelf NAMES readelf llvm-readelf REQUIRED)
    file(GLOB _linux_binding_modules
        "${_verify_root}/Infernux/lib/_Infernux*.so"
    )
    foreach(_linux_binding_module IN LISTS _linux_binding_modules)
        execute_process(
            COMMAND "${_infernux_readelf}" -d "${_linux_binding_module}"
            RESULT_VARIABLE _readelf_result
            OUTPUT_VARIABLE _dynamic_section
            ERROR_VARIABLE _readelf_error
        )
        if(NOT _readelf_result EQUAL 0)
            message(FATAL_ERROR
                "Unable to inspect Linux wheel module ${_linux_binding_module}: "
                "${_readelf_error}"
            )
        endif()
        if(_dynamic_section MATCHES "Shared library: \\[libpython[^]]+\\]")
            message(FATAL_ERROR
                "Linux wheel module has a direct libpython dependency and will "
                "not relocate into an ordinary venv: ${_linux_binding_module}"
            )
        endif()
    endforeach()
endif()

file(REMOVE_RECURSE "${_verify_root}")
message(STATUS "Verified native payload for ${_wheel}")
