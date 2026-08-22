if(NOT DEFINED PYTHON_EXECUTABLE OR PYTHON_EXECUTABLE STREQUAL "")
    message(FATAL_ERROR "PYTHON_EXECUTABLE is required")
endif()

execute_process(
    COMMAND "${PYTHON_EXECUTABLE}" -m pip --version
    RESULT_VARIABLE _pip_result
    OUTPUT_QUIET
    ERROR_QUIET
)

if(NOT _pip_result EQUAL 0)
    message(FATAL_ERROR
        "Python interpreter '${PYTHON_EXECUTABLE}' does not have pip available. "
        "Install pip for this interpreter before building.")
endif()

execute_process(
    COMMAND "${PYTHON_EXECUTABLE}" -c
        "import importlib.metadata as m; import build, wheel, setuptools.build_meta; parts=lambda v: tuple(int(p) for p in v.split('.')[:2]); raise SystemExit(0 if parts(m.version('setuptools')) >= (70, 1) else 1)"
    RESULT_VARIABLE _tools_ready
    OUTPUT_QUIET
    ERROR_QUIET
)

if(_tools_ready EQUAL 0)
    message(STATUS "Python packaging tools are already available; skipping network bootstrap")
    return()
endif()

message(STATUS "Python packaging tools are missing or outdated; installing required versions")
execute_process(
    COMMAND "${PYTHON_EXECUTABLE}" -m pip install --disable-pip-version-check
        "build>=1.2" "wheel>=0.43" "setuptools>=70.1"
    RESULT_VARIABLE _bootstrap_result
    COMMAND_ECHO STDOUT
)

if(NOT _bootstrap_result EQUAL 0)
    message(FATAL_ERROR
        "Failed to install required Python packaging tools (build, wheel, setuptools) "
        "for interpreter '${PYTHON_EXECUTABLE}'.")
endif()
