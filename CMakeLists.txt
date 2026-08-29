cmake_minimum_required(VERSION 3.25)

# Resolve platform compiler companion tools before project() performs its IPO
# probe. Ubuntu installs versioned LLVM binaries without always publishing the
# unversioned llvm-ar/llvm-ranlib names expected by CMake's Clang integration.
include("${CMAKE_CURRENT_LIST_DIR}/cmake/InfernuxCompilerTools.cmake")

project(Infernux LANGUAGES C CXX)

# Root orchestration only. Target ownership and packaging policy live in the
# focused modules below so platform bring-up does not keep expanding this file.
include("${CMAKE_SOURCE_DIR}/cmake/InfernuxBuildOptions.cmake")
include("${CMAKE_SOURCE_DIR}/cmake/InfernuxSources.cmake")

add_subdirectory(external)

include("${CMAKE_SOURCE_DIR}/cmake/InfernuxNativeTargets.cmake")
include("${CMAKE_SOURCE_DIR}/cmake/InfernuxPlayerHost.cmake")
add_subdirectory(external/plugins)
include("${CMAKE_SOURCE_DIR}/cpp/tests/CMakeLists.txt")
include("${CMAKE_SOURCE_DIR}/cmake/InfernuxInstall.cmake")
include("${CMAKE_SOURCE_DIR}/cmake/InfernuxHub.cmake")
include("${CMAKE_SOURCE_DIR}/cmake/InfernuxDeveloperTools.cmake")
include("${CMAKE_SOURCE_DIR}/cmake/InfernuxPackaging.cmake")
