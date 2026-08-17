if(NOT DEFINED SOURCE OR NOT DEFINED DESTINATION)
    message(FATAL_ERROR "stage_build_file.cmake requires SOURCE and DESTINATION")
endif()

include("${CMAKE_CURRENT_LIST_DIR}/stable_copy.cmake")
infernux_copy_file_stable("${SOURCE}" "${DESTINATION}")
