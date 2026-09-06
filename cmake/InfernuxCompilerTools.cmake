# Compiler companion tools needed while project() identifies the toolchain.

if(NOT CMAKE_HOST_LINUX)
    return()
endif()

if(NOT CMAKE_C_COMPILER MATCHES "(^|[/\\\\])clang(-[0-9]+)?$"
   AND NOT CMAKE_CXX_COMPILER MATCHES "(^|[/\\\\])clang\\+\\+(-[0-9]+)?$")
    return()
endif()

find_program(INFERNUX_LLVM_AR_EXECUTABLE
    NAMES llvm-ar llvm-ar-22 llvm-ar-21 llvm-ar-20 llvm-ar-19 llvm-ar-18 llvm-ar-17
)
find_program(INFERNUX_LLVM_RANLIB_EXECUTABLE
    NAMES llvm-ranlib llvm-ranlib-22 llvm-ranlib-21 llvm-ranlib-20
          llvm-ranlib-19 llvm-ranlib-18 llvm-ranlib-17
)

if(NOT INFERNUX_LLVM_AR_EXECUTABLE OR NOT INFERNUX_LLVM_RANLIB_EXECUTABLE)
    message(FATAL_ERROR
        "Clang IPO/LTO requires llvm-ar and llvm-ranlib. On Ubuntu, install "
        "the llvm package or run scripts/setup/install_linux_dependencies.sh."
    )
endif()

# CMake's Clang compiler detection asks PATH for the unversioned tool names
# even when a versioned executable was supplied through the cache. Publish
# build-local links so distro-specific suffixes do not leak into presets.
set(_infernux_llvm_tool_dir "${CMAKE_BINARY_DIR}/toolchain-bin")
file(MAKE_DIRECTORY "${_infernux_llvm_tool_dir}")
foreach(_tool IN ITEMS llvm-ar llvm-ranlib)
    if(_tool STREQUAL "llvm-ar")
        set(_source "${INFERNUX_LLVM_AR_EXECUTABLE}")
    else()
        set(_source "${INFERNUX_LLVM_RANLIB_EXECUTABLE}")
    endif()
    set(_destination "${_infernux_llvm_tool_dir}/${_tool}")
    file(REMOVE "${_destination}")
    file(CREATE_LINK "${_source}" "${_destination}" SYMBOLIC COPY_ON_ERROR)
endforeach()
set(ENV{PATH} "${_infernux_llvm_tool_dir}:$ENV{PATH}")

set(CMAKE_AR "${INFERNUX_LLVM_AR_EXECUTABLE}" CACHE FILEPATH "LLVM archiver" FORCE)
set(CMAKE_RANLIB "${INFERNUX_LLVM_RANLIB_EXECUTABLE}" CACHE FILEPATH "LLVM ranlib" FORCE)
set(CMAKE_C_COMPILER_AR "${INFERNUX_LLVM_AR_EXECUTABLE}" CACHE FILEPATH "Clang C archiver" FORCE)
set(CMAKE_CXX_COMPILER_AR "${INFERNUX_LLVM_AR_EXECUTABLE}" CACHE FILEPATH "Clang C++ archiver" FORCE)
set(CMAKE_C_COMPILER_RANLIB "${INFERNUX_LLVM_RANLIB_EXECUTABLE}" CACHE FILEPATH "Clang C ranlib" FORCE)
set(CMAKE_CXX_COMPILER_RANLIB "${INFERNUX_LLVM_RANLIB_EXECUTABLE}" CACHE FILEPATH "Clang C++ ranlib" FORCE)

unset(_destination)
unset(_infernux_llvm_tool_dir)
unset(_source)
unset(_tool)
