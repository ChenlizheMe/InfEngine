# Global build policy and host toolchain discovery.

if(MSVC)
    # Limit /MP globally, including third-party projects. CMake maps /MP to
    # MultiProcessorCompilation and the actual worker cap comes from this
    # Visual Studio global property.
    set(INFERNUX_MSVC_COMPILE_JOBS "4" CACHE STRING
        "Maximum MSVC translation units compiled concurrently per project")
    list(APPEND CMAKE_VS_GLOBALS "CL_MPCount=${INFERNUX_MSVC_COMPILE_JOBS}")
endif()

# Static third-party libraries feed shared Infernux targets on Unix hosts.
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(INFERNUX_PYTHON_SYNC_DIR "${CMAKE_BINARY_DIR}/python-sync" CACHE PATH
    "Build-tree directory that receives the native Python runtime")
set(PYTHON_TARGET_DIR "${INFERNUX_PYTHON_SYNC_DIR}")

set(PYBIND11_FINDPYTHON ON)
include("${CMAKE_SOURCE_DIR}/cmake/InfernuxPython.cmake")
find_package(Vulkan REQUIRED)

option(INFERNUX_BUILD_PLAYER_HOST
    "Build the native single-process PlayerHost bootstrap"
    OFF
)
option(INFERNUX_RUNTIME_STATIC
    "Link the private runtime composition layer statically into the Python module"
    ON
)
option(INFERNUX_RELEASE_LTO "Enable IPO/LTO for native Release targets" ON)
option(INFERNUX_FRAME_PROFILE_TERMINAL "Emit bounded frame-profile reports to stderr" OFF)

# Vulkan validation is enabled for development configurations by default, but
# remains explicitly overridable independently from optimization policy.
set(INFERNUX_ENABLE_VULKAN_VALIDATION "AUTO" CACHE STRING
    "Vulkan validation policy: AUTO (Debug/RelWithDebInfo on, Release off), ON, or OFF")
set_property(CACHE INFERNUX_ENABLE_VULKAN_VALIDATION PROPERTY STRINGS AUTO ON OFF)
string(TOUPPER "${INFERNUX_ENABLE_VULKAN_VALIDATION}" _infernux_vulkan_validation_policy)
if(NOT _infernux_vulkan_validation_policy MATCHES "^(AUTO|ON|OFF)$")
    message(FATAL_ERROR
        "INFERNUX_ENABLE_VULKAN_VALIDATION must be AUTO, ON, or OFF; got '${INFERNUX_ENABLE_VULKAN_VALIDATION}'")
endif()

if(_infernux_vulkan_validation_policy STREQUAL "ON")
    set(_infernux_vulkan_validation_non_release "1")
elseif(_infernux_vulkan_validation_policy STREQUAL "OFF")
    set(_infernux_vulkan_validation_non_release "0")
else()
    set(_infernux_vulkan_validation_non_release
        "$<IF:$<OR:$<CONFIG:Debug>,$<CONFIG:RelWithDebInfo>>,1,0>")
endif()

# Shipping configurations always disable Vulkan validation. The cache policy
# controls only non-shipping configurations.
set(_infernux_vulkan_validation_definition
    "INFERNUX_VULKAN_VALIDATION_LAYERS=$<IF:$<OR:$<CONFIG:Release>,$<CONFIG:MinSizeRel>>,0,${_infernux_vulkan_validation_non_release}>")
