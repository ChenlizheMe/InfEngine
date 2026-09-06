# Opt-in CI install rule. Public platform releases use the normal preset and
# never include the software test driver. CMake's project hook defers this rule
# until the root project has declared its PythonWheel install component.
function(_infernux_install_ci_vulkan)
    install(FILES
        "${CMAKE_SOURCE_DIR}/out/toolchains/windows-swiftshader/runtime/vk_swiftshader.dll"
        DESTINATION "python/Infernux/lib"
        RENAME "vulkan-1.dll"
        COMPONENT ${INFERNUX_PYTHON_INSTALL_COMPONENT})
endfunction()
cmake_language(DEFER CALL _infernux_install_ci_vulkan)
