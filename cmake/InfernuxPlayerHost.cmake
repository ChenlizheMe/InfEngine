# Native packaged-game bootstrap shared by Windows and Linux desktop Players.

if(NOT INFERNUX_BUILD_PLAYER_HOST)
    return()
endif()

if(WIN32)
    include(InstallRequiredSystemLibraries)
endif()

add_executable(InfernuxPlayerHost
    cpp/infernux/tools/launcher/InfernuxPlayerLauncher.cpp
    cpp/infernux/tools/launcher/PlayerHost.cpp
    cpp/infernux/tools/launcher/PlayerHost.h
)
if(WIN32)
    target_sources(InfernuxPlayerHost PRIVATE
        cpp/infernux/tools/launcher/InfernuxPlayerLauncher.rc
    )
    set_target_properties(InfernuxPlayerHost PROPERTIES WIN32_EXECUTABLE TRUE)
endif()
set(INFERNUX_PLAYER_HOST_DIR
    "${CMAKE_BINARY_DIR}/player-runtime"
)
set_target_properties(InfernuxPlayerHost PROPERTIES
    OUTPUT_NAME "InfernuxPlayerHost"
    MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
    RUNTIME_OUTPUT_DIRECTORY "${INFERNUX_PLAYER_HOST_DIR}"
    RUNTIME_OUTPUT_DIRECTORY_RELEASE "${INFERNUX_PLAYER_HOST_DIR}"
    RUNTIME_OUTPUT_DIRECTORY_RELWITHDEBINFO "${INFERNUX_PLAYER_HOST_DIR}"
    PDB_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/symbols"
    PDB_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}/symbols"
    PDB_OUTPUT_DIRECTORY_RELWITHDEBINFO "${CMAKE_BINARY_DIR}/symbols"
    COMPILE_PDB_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/symbols"
    COMPILE_PDB_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}/symbols"
    COMPILE_PDB_OUTPUT_DIRECTORY_RELWITHDEBINFO "${CMAKE_BINARY_DIR}/symbols"
)

# prebuild_player_runtime publishes this target with the complete Player payload
# into its platform plugin. Do not also copy it into the Editor's source resources.
if(MSVC)
    target_compile_options(InfernuxPlayerHost PRIVATE /utf-8 /O1)
    # Python's Windows headers inject the import library. PlayerHost resolves
    # every PEP 587 symbol from the Python DLL at runtime instead.
    target_link_options(InfernuxPlayerHost PRIVATE
        "/NODEFAULTLIB:python${Python3_VERSION_MAJOR}${Python3_VERSION_MINOR}.lib"
    )
endif()
target_include_directories(InfernuxPlayerHost PRIVATE
    ${CMAKE_SOURCE_DIR}/cpp
    ${CMAKE_SOURCE_DIR}/cpp/infernux
    ${CMAKE_SOURCE_DIR}/external
    ${INFERNUX_PYTHON_INCLUDE_DIRS}
)
target_link_libraries(InfernuxPlayerHost PRIVATE ${CMAKE_DL_LIBS})
if(WIN32)
    target_link_libraries(InfernuxPlayerHost PRIVATE shell32)
else()
    set_target_properties(InfernuxPlayerHost PROPERTIES
        BUILD_RPATH "$ORIGIN"
        INSTALL_RPATH "$ORIGIN"
    )
endif()
set(INFERNUX_PLAYER_HOST_BUILD_PATH "$<TARGET_FILE:InfernuxPlayerHost>")
