# Host-tool and embedded-target Python discovery.

set(INFERNUX_HOST_PYTHON_EXECUTABLE "" CACHE FILEPATH
    "Build-machine Python used by generators and packaging tools")
option(INFERNUX_USE_TARGET_PYTHON
    "Link the engine against an explicitly supplied target Python runtime"
    OFF
)
set(INFERNUX_TARGET_PYTHON_INCLUDE_DIR "" CACHE PATH
    "Target-platform directory containing Python.h")
set(INFERNUX_TARGET_PYTHON_LIBRARY "" CACHE FILEPATH
    "Target-platform libpython shared library")

if(INFERNUX_HOST_PYTHON_EXECUTABLE)
    set(Python3_EXECUTABLE "${INFERNUX_HOST_PYTHON_EXECUTABLE}" CACHE FILEPATH
        "Host Python interpreter" FORCE)
endif()

if(INFERNUX_USE_TARGET_PYTHON)
    find_package(Python3 3.13 EXACT COMPONENTS Interpreter REQUIRED)
    set(INFERNUX_HOST_PYTHON_EXECUTABLE "${Python3_EXECUTABLE}" CACHE FILEPATH
        "Build-machine Python used by generators and packaging tools" FORCE)

    if(NOT IS_DIRECTORY "${INFERNUX_TARGET_PYTHON_INCLUDE_DIR}" OR
       NOT EXISTS "${INFERNUX_TARGET_PYTHON_INCLUDE_DIR}/Python.h")
        message(FATAL_ERROR
            "INFERNUX_USE_TARGET_PYTHON requires "
            "INFERNUX_TARGET_PYTHON_INCLUDE_DIR containing Python.h")
    endif()
    if(NOT EXISTS "${INFERNUX_TARGET_PYTHON_LIBRARY}" OR
       IS_DIRECTORY "${INFERNUX_TARGET_PYTHON_LIBRARY}")
        message(FATAL_ERROR
            "INFERNUX_USE_TARGET_PYTHON requires INFERNUX_TARGET_PYTHON_LIBRARY")
    endif()

    add_library(InfernuxTargetPython SHARED IMPORTED GLOBAL)
    set_target_properties(InfernuxTargetPython PROPERTIES
        IMPORTED_LOCATION "${INFERNUX_TARGET_PYTHON_LIBRARY}"
        INTERFACE_INCLUDE_DIRECTORIES "${INFERNUX_TARGET_PYTHON_INCLUDE_DIR}"
    )
    set(INFERNUX_PYTHON_INCLUDE_DIRS "${INFERNUX_TARGET_PYTHON_INCLUDE_DIR}")
else()
    find_package(Python3 3.13 EXACT COMPONENTS Interpreter Development.Module Development.Embed REQUIRED)
    set(INFERNUX_HOST_PYTHON_EXECUTABLE "${Python3_EXECUTABLE}" CACHE FILEPATH
        "Build-machine Python used by generators and packaging tools" FORCE)
    set(INFERNUX_PYTHON_INCLUDE_DIRS "${Python3_INCLUDE_DIRS}")
endif()

# pybind11 is a build dependency. Resolve its CMake package with the host
# interpreter even when the produced module links to a different target Python.
execute_process(
    COMMAND "${INFERNUX_HOST_PYTHON_EXECUTABLE}" -m pybind11 --cmakedir
    OUTPUT_VARIABLE _pybind11_cmakedir
    OUTPUT_STRIP_TRAILING_WHITESPACE
    ERROR_QUIET
    RESULT_VARIABLE _pybind11_result
)
if(_pybind11_result EQUAL 0 AND _pybind11_cmakedir)
    # pybind11 is a host-side header/build dependency. Android and other cross
    # toolchains commonly root CMAKE_PREFIX_PATH in the target sysroot, so an
    # appended host path can become invisible. Search the directory reported by
    # the host interpreter explicitly instead.
    find_package(pybind11 CONFIG REQUIRED
        PATHS "${_pybind11_cmakedir}"
        NO_DEFAULT_PATH
    )
else()
    find_package(pybind11 CONFIG REQUIRED)
endif()
