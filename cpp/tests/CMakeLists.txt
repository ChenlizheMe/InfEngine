# ------------------------------------------------------------------------------
# Native regression tests
# ------------------------------------------------------------------------------
option(INFERNUX_BUILD_TESTS "Build Infernux native regression tests" ON)
if(INFERNUX_BUILD_TESTS)
    enable_testing()
    find_package(Threads REQUIRED)

    # Native test executables are standalone CPython hosts whenever they pull
    # the full InfernuxRuntime graph.  Keep this linkage out of the extension
    # module itself so Linux wheels do not gain a creator-prefix libpython
    # dependency, while every executable gets the symbols LTO may expose.
    add_library(infernux_runtime_test_support INTERFACE)
    target_link_libraries(infernux_runtime_test_support INTERFACE
        InfernuxRuntime
        pybind11::embed
    )

    add_executable(infernux_path_tests
        cpp/tests/PathTests.cpp
    )
    target_include_directories(infernux_path_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_path_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.path COMMAND infernux_path_tests)

    add_executable(infernux_inxpack_tests
        cpp/tests/InxPackTests.cpp
    )
    target_include_directories(infernux_inxpack_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_inxpack_tests PRIVATE InfernuxFoundation)
    if(MSVC)
        target_compile_options(infernux_inxpack_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.inxpack COMMAND infernux_inxpack_tests)

    add_executable(infernux_player_host_tests
        cpp/tests/PlayerHostTests.cpp
        cpp/infernux/tools/launcher/PlayerHost.cpp
        cpp/infernux/tools/launcher/PlayerHost.h
    )
    target_include_directories(infernux_player_host_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
        ${Python3_INCLUDE_DIRS}
    )
    target_link_libraries(infernux_player_host_tests PRIVATE ${CMAKE_DL_LIBS})
    if(WIN32)
        target_link_libraries(infernux_player_host_tests PRIVATE shell32)
        set_target_properties(infernux_player_host_tests PROPERTIES
            MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
        )
    endif()
    if(MSVC)
        target_compile_options(infernux_player_host_tests PRIVATE /utf-8)
        target_link_options(infernux_player_host_tests PRIVATE
            "/NODEFAULTLIB:python${Python3_VERSION_MAJOR}${Python3_VERSION_MINOR}.lib"
        )
    endif()
    add_test(NAME infernux.player_host COMMAND infernux_player_host_tests)

    add_executable(infernux_editor_search_model_tests
        cpp/tests/EditorSearchModelTests.cpp
    )
    target_include_directories(infernux_editor_search_model_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_editor_search_model_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.editor_search_model COMMAND infernux_editor_search_model_tests)

    add_executable(infernux_editor_collection_model_tests
        cpp/tests/EditorCollectionModelTests.cpp
    )
    target_include_directories(infernux_editor_collection_model_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_editor_collection_model_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.editor_collection_model COMMAND infernux_editor_collection_model_tests)

    add_executable(infernux_editor_theme_registry_tests
        cpp/tests/EditorThemeRegistryTests.cpp
        cpp/infernux/function/editor/EditorThemeRegistry.cpp
    )
    target_include_directories(infernux_editor_theme_registry_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external/imgui
    )
    target_link_libraries(infernux_editor_theme_registry_tests PRIVATE imgui)
    if(MSVC)
        target_compile_options(infernux_editor_theme_registry_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.editor_theme_registry COMMAND infernux_editor_theme_registry_tests)

    add_executable(infernux_editor_window_bounds_tests cpp/tests/EditorWindowBoundsTests.cpp)
    target_include_directories(infernux_editor_window_bounds_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external/imgui
    )
    target_link_libraries(infernux_editor_window_bounds_tests PRIVATE imgui)
    if(MSVC)
        target_compile_options(infernux_editor_window_bounds_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_editor_window_bounds_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.editor_window_bounds COMMAND infernux_editor_window_bounds_tests)

    add_executable(infernux_job_system_tests
        cpp/tests/JobSystemTests.cpp
        cpp/infernux/core/log/InxLog.cpp
        cpp/infernux/core/threading/JobSystem.cpp
    )
    target_include_directories(infernux_job_system_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    target_link_libraries(infernux_job_system_tests PRIVATE Threads::Threads)
    if(MSVC)
        target_compile_options(infernux_job_system_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.job_system COMMAND infernux_job_system_tests)
    set_tests_properties(infernux.job_system PROPERTIES TIMEOUT 30)

    add_executable(infernux_jolt_job_system_adapter_tests
        cpp/tests/InfernuxJoltJobSystemAdapterTests.cpp
        cpp/infernux/function/scene/physics/InfernuxJoltJobSystemAdapter.cpp
        cpp/infernux/core/log/InxLog.cpp
        cpp/infernux/core/threading/JobSystem.cpp
    )
    target_include_directories(infernux_jolt_job_system_adapter_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_jolt_job_system_adapter_tests PRIVATE Jolt Threads::Threads)
    if(MSVC)
        target_compile_options(infernux_jolt_job_system_adapter_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.jolt_job_system_adapter COMMAND infernux_jolt_job_system_adapter_tests)
    set_tests_properties(infernux.jolt_job_system_adapter PROPERTIES TIMEOUT 30)

    add_executable(infernux_gpu_retirement_queue_tests
        cpp/tests/GpuRetirementQueueTests.cpp
    )
    target_include_directories(infernux_gpu_retirement_queue_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_gpu_retirement_queue_tests PRIVATE /utf-8)
    endif()
    target_link_libraries(infernux_gpu_retirement_queue_tests PRIVATE InfernuxRenderCore)
    add_test(NAME infernux.gpu_retirement_queue COMMAND infernux_gpu_retirement_queue_tests)

    add_executable(infernux_editor_gui_frame_scheduler_tests
        cpp/tests/EditorGuiFrameSchedulerTests.cpp
    )
    target_include_directories(infernux_editor_gui_frame_scheduler_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_editor_gui_frame_scheduler_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.editor_gui_frame_scheduler COMMAND infernux_editor_gui_frame_scheduler_tests)

    add_executable(infernux_log_release_policy_tests
        cpp/tests/InxLogReleasePolicyTests.cpp
        cpp/infernux/core/log/InxLog.cpp
    )
    target_include_directories(infernux_log_release_policy_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    target_compile_definitions(infernux_log_release_policy_tests PRIVATE
        INFERNUX_COMPILE_OUT_DEBUG_LOGS=1
    )
    if(MSVC)
        target_compile_options(infernux_log_release_policy_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.log_release_policy COMMAND infernux_log_release_policy_tests)

    add_executable(infernux_input_manager_tests
        cpp/tests/InputManagerTests.cpp
        cpp/infernux/platform/input/InputManager.cpp
        cpp/infernux/core/log/InxLog.cpp
    )
    target_include_directories(infernux_input_manager_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    target_link_libraries(infernux_input_manager_tests PRIVATE SDL3::SDL3)
    if(MSVC)
        target_compile_options(infernux_input_manager_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.input_manager COMMAND infernux_input_manager_tests)

    add_executable(infernux_window_sizing_policy_tests
        cpp/tests/WindowSizingPolicyTests.cpp
    )
    target_include_directories(infernux_window_sizing_policy_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_window_sizing_policy_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.window_sizing_policy COMMAND infernux_window_sizing_policy_tests)

    if(WIN32)
        add_executable(infernux_windows_dpi_policy_tests
            cpp/tests/WindowsDpiPolicyTests.cpp
            cpp/infernux/platform/window/WindowsDpiPolicy.cpp
        )
        target_include_directories(infernux_windows_dpi_policy_tests PRIVATE
            ${CMAKE_SOURCE_DIR}/cpp/infernux
        )
        target_link_libraries(infernux_windows_dpi_policy_tests PRIVATE SDL3::SDL3)
        if(MSVC)
            target_compile_options(infernux_windows_dpi_policy_tests PRIVATE /utf-8)
        endif()
        add_test(NAME infernux.windows_dpi_policy COMMAND infernux_windows_dpi_policy_tests)
    endif()

    add_executable(infernux_atomic_file_tests
        cpp/tests/AtomicFileTests.cpp
        cpp/infernux/platform/filesystem/AtomicFile.cpp
    )
    target_include_directories(infernux_atomic_file_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_atomic_file_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.atomic_file COMMAND infernux_atomic_file_tests)

    add_executable(infernux_document_store_tests
        cpp/tests/DocumentStoreTests.cpp
        cpp/infernux/platform/filesystem/DocumentStore.cpp
        cpp/infernux/platform/filesystem/AtomicFile.cpp
    )
    target_include_directories(infernux_document_store_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_document_store_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.document_store COMMAND infernux_document_store_tests)

    add_executable(infernux_document_transaction_tests
        cpp/tests/DocumentTransactionTests.cpp
        cpp/infernux/platform/filesystem/DocumentTransaction.cpp
        cpp/infernux/platform/filesystem/DocumentStore.cpp
        cpp/infernux/platform/filesystem/AtomicFile.cpp
    )
    target_include_directories(infernux_document_transaction_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    target_link_libraries(infernux_document_transaction_tests PRIVATE Threads::Threads)
    target_link_libraries(infernux_document_transaction_tests PRIVATE ${INFERNUX_ZLIB_TARGET})
    if(INFERNUX_ZLIB_INCLUDE_DIRS)
        target_include_directories(infernux_document_transaction_tests PRIVATE ${INFERNUX_ZLIB_INCLUDE_DIRS})
    endif()
    if(MSVC)
        target_compile_options(infernux_document_transaction_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.document_transaction COMMAND infernux_document_transaction_tests)

    add_executable(infernux_component_data_store_tests
        cpp/tests/ComponentDataStoreTests.cpp
        cpp/infernux/function/scene/ComponentDataStore.cpp
    )
    target_include_directories(infernux_component_data_store_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_component_data_store_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_component_data_store_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.component_data_store COMMAND infernux_component_data_store_tests)

    add_executable(infernux_component_document_validation_tests
        cpp/tests/ComponentDocumentValidationTests.cpp
        cpp/infernux/function/scene/ComponentDocumentValidation.cpp
    )
    target_include_directories(infernux_component_document_validation_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    if(MSVC)
        target_compile_options(infernux_component_document_validation_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.component_document_validation COMMAND infernux_component_document_validation_tests)

    add_executable(infernux_render_identity_tests
        cpp/tests/RenderIdentityTests.cpp
    )
    target_include_directories(infernux_render_identity_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_render_identity_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.render_identity COMMAND infernux_render_identity_tests)

    add_executable(infernux_canonical_light_snapshot_tests
        cpp/tests/CanonicalLightSnapshotTests.cpp
        cpp/infernux/function/renderer/lighting/CanonicalLightGpuBuffer.cpp
        cpp/infernux/function/renderer/lighting/ForwardPlusLightGrid.cpp
    )
    target_include_directories(infernux_canonical_light_snapshot_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_canonical_light_snapshot_tests PRIVATE glm::glm)
    # Match the runtime GLM conventions so shadow math is tested under the
    # exact same handedness and clip-depth range the engine renders with.
    target_compile_definitions(infernux_canonical_light_snapshot_tests PRIVATE
        GLM_FORCE_DEPTH_ZERO_TO_ONE
        GLM_FORCE_LEFT_HANDED
    )
    if(MSVC)
        target_compile_options(infernux_canonical_light_snapshot_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.canonical_light_snapshot COMMAND infernux_canonical_light_snapshot_tests)

    add_executable(infernux_render_instance_history_tests
        cpp/tests/RenderInstanceHistoryTests.cpp
    )
    target_include_directories(infernux_render_instance_history_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_render_instance_history_tests PRIVATE glm::glm)
    if(MSVC)
        target_compile_options(infernux_render_instance_history_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.render_instance_history COMMAND infernux_render_instance_history_tests)

    add_executable(infernux_material_pass_pipeline_tests
        cpp/tests/MaterialPassPipelineTests.cpp
    )
    target_include_directories(infernux_material_pass_pipeline_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_material_pass_pipeline_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.material_pass_pipeline COMMAND infernux_material_pass_pipeline_tests)

    add_executable(infernux_render_graph_identity_tests
        cpp/tests/RenderGraphIdentityTests.cpp
    )
    target_include_directories(infernux_render_graph_identity_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_render_graph_identity_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.render_graph_identity COMMAND infernux_render_graph_identity_tests)

    add_executable(infernux_rhi_query_tests
        cpp/tests/RhiQueryTests.cpp
    )
    target_include_directories(infernux_rhi_query_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_rhi_query_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.rhi_query COMMAND infernux_rhi_query_tests)

    add_executable(infernux_rhi_capabilities_tests
        cpp/tests/RhiCapabilitiesTests.cpp
    )
    target_include_directories(infernux_rhi_capabilities_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_rhi_capabilities_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.rhi_capabilities COMMAND infernux_rhi_capabilities_tests)

    add_executable(infernux_rhi_resource_index_tests
        cpp/tests/RhiResourceIndexTests.cpp
    )
    target_include_directories(infernux_rhi_resource_index_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_rhi_resource_index_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.rhi_resource_index COMMAND infernux_rhi_resource_index_tests)

    add_executable(infernux_msaa_policy_tests
        cpp/tests/MsaaPolicyTests.cpp
    )
    target_include_directories(infernux_msaa_policy_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_msaa_policy_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.msaa_policy COMMAND infernux_msaa_policy_tests)

    add_executable(infernux_frustum_tests
        cpp/tests/FrustumTests.cpp
    )
    target_include_directories(infernux_frustum_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    target_link_libraries(infernux_frustum_tests PRIVATE glm::glm)
    target_compile_definitions(infernux_frustum_tests PRIVATE
        GLM_FORCE_DEPTH_ZERO_TO_ONE
        GLM_FORCE_LEFT_HANDED
    )
    if(MSVC)
        target_compile_options(infernux_frustum_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.frustum COMMAND infernux_frustum_tests)

    add_executable(infernux_rhi_contracts_tests
        cpp/tests/RhiContractsTests.cpp
    )
    target_include_directories(infernux_rhi_contracts_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    target_link_libraries(infernux_rhi_contracts_tests PRIVATE InfernuxRenderCore)
    if(MSVC)
        target_compile_options(infernux_rhi_contracts_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_rhi_contracts_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.rhi_contracts COMMAND infernux_rhi_contracts_tests)

    add_executable(infernux_vulkan_rhi_device_tests
        cpp/tests/VulkanRhiDeviceTests.cpp
        cpp/infernux/core/log/InxLog.cpp
        cpp/infernux/function/renderer/rhi/RhiDeviceIdentity.cpp
        cpp/infernux/function/renderer/vk/DescriptorBindTrace.cpp
        cpp/infernux/function/renderer/vk/VmaContext.cpp
        cpp/infernux/function/renderer/vk/VkDescriptorManager.cpp
        cpp/infernux/function/renderer/vk/VulkanBindlessTextureTable.cpp
        cpp/infernux/function/renderer/vk/VulkanRhiDevice.cpp
    )
    target_include_directories(infernux_vulkan_rhi_device_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    target_link_libraries(infernux_vulkan_rhi_device_tests PRIVATE
        Vulkan::Vulkan
        GPUOpen::VulkanMemoryAllocator
    )
    if(MSVC)
        target_compile_options(infernux_vulkan_rhi_device_tests PRIVATE /utf-8)
        target_link_libraries(infernux_vulkan_rhi_device_tests PRIVATE delayimp)
        target_link_options(infernux_vulkan_rhi_device_tests PRIVATE /DELAYLOAD:vulkan-1.dll)
    endif()
    add_test(NAME infernux.vulkan_rhi_device COMMAND infernux_vulkan_rhi_device_tests)
    set_tests_properties(infernux.vulkan_rhi_device PROPERTIES TIMEOUT 30)

    add_executable(infernux_vulkan_submission_executor_tests
        cpp/tests/VulkanSubmissionExecutorTests.cpp
    )
    target_include_directories(infernux_vulkan_submission_executor_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    target_link_libraries(infernux_vulkan_submission_executor_tests PRIVATE InfernuxVulkanBackend)
    if(MSVC)
        target_compile_options(infernux_vulkan_submission_executor_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_vulkan_submission_executor_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.vulkan_submission_executor COMMAND infernux_vulkan_submission_executor_tests)
    set_tests_properties(infernux.vulkan_submission_executor PROPERTIES
        LABELS "requires_vulkan_device"
        TIMEOUT 60
    )

    add_executable(infernux_scene_render_graph_validation_tests
        cpp/tests/SceneRenderGraphValidationTests.cpp
    )
    target_include_directories(infernux_scene_render_graph_validation_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    # SceneRenderGraph validation now reaches the full Scene/GameObject graph,
    # which includes PyComponentProxy.  Standalone executables must provide the
    # CPython host symbols explicitly on ELF; the extension-module build leaves
    # those symbols to its already-running interpreter by design.
    target_link_libraries(infernux_scene_render_graph_validation_tests PRIVATE
        infernux_runtime_test_support
    )
    if(MSVC)
        target_compile_options(infernux_scene_render_graph_validation_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.scene_render_graph_validation COMMAND infernux_scene_render_graph_validation_tests)

    add_executable(infernux_py_component_proxy_tests
        cpp/tests/PyComponentProxyTests.cpp
    )
    target_include_directories(infernux_py_component_proxy_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_py_component_proxy_tests PRIVATE
        infernux_runtime_test_support
    )
    if(MSVC)
        target_compile_options(infernux_py_component_proxy_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.py_component_proxy COMMAND infernux_py_component_proxy_tests)
    set_tests_properties(infernux.py_component_proxy PROPERTIES
        ENVIRONMENT "PYTHONPATH=${CMAKE_SOURCE_DIR}/python;INFERNUX_NATIVE_MODULE_DIR=$<TARGET_FILE_DIR:_Infernux>"
    )

    add_executable(infernux_runtime_frame_barrier_tests
        cpp/tests/RuntimeFrameBarrierTests.cpp
    )
    target_include_directories(infernux_runtime_frame_barrier_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_runtime_frame_barrier_tests PRIVATE infernux_runtime_test_support)
    if(MSVC)
        target_compile_options(infernux_runtime_frame_barrier_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_runtime_frame_barrier_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.runtime_frame_barrier COMMAND infernux_runtime_frame_barrier_tests)

    add_executable(infernux_runtime_persistent_scene_tests
        cpp/tests/RuntimePersistentSceneTests.cpp
    )
    target_include_directories(infernux_runtime_persistent_scene_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_runtime_persistent_scene_tests PRIVATE infernux_runtime_test_support)
    if(MSVC)
        target_compile_options(infernux_runtime_persistent_scene_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_runtime_persistent_scene_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.runtime_persistent_scene COMMAND infernux_runtime_persistent_scene_tests)

    add_executable(infernux_camera_culling_isolation_tests
        cpp/tests/CameraCullingIsolationTests.cpp
    )
    target_include_directories(infernux_camera_culling_isolation_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_camera_culling_isolation_tests PRIVATE infernux_runtime_test_support)
    if(MSVC)
        target_compile_options(infernux_camera_culling_isolation_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_camera_culling_isolation_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.camera_culling_isolation COMMAND infernux_camera_culling_isolation_tests)

    add_executable(infernux_particle_gpu_runtime_tests
        cpp/tests/ParticleGpuRuntimeTests.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuBillboardRenderer.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuSurfaceBinding.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuMeshRenderer.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuBounds.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuContinuationRuntime.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuContactRuntime.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuCollisionScene.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuDrawRegistry.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuMigrator.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuRibbonRenderer.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuRibbonTopology.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuRuntime.cpp
    )
    target_include_directories(infernux_particle_gpu_runtime_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_particle_gpu_runtime_tests PRIVATE infernux_runtime_test_support)
    if(MSVC)
        target_compile_options(infernux_particle_gpu_runtime_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_particle_gpu_runtime_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.particle_gpu_runtime COMMAND infernux_particle_gpu_runtime_tests)

    set(INFERNUX_COMPUTE_INDIRECT_SHADER_DIR "${CMAKE_CURRENT_BINARY_DIR}/tests/shaders")
    set(INFERNUX_COMPUTE_INDIRECT_SHADER_SOURCES
        "${CMAKE_SOURCE_DIR}/cpp/tests/shaders/RenderGraphComputeIndirect.comp"
        "${CMAKE_SOURCE_DIR}/cpp/tests/shaders/RenderGraphComputeIndirect.vert"
        "${CMAKE_SOURCE_DIR}/cpp/tests/shaders/RenderGraphComputeIndirect.frag"
        "${CMAKE_SOURCE_DIR}/cpp/tests/shaders/ShaderReflectionStorage.comp"
        "${CMAKE_SOURCE_DIR}/cpp/tests/shaders/ParticleGpuFixture.comp"
        "${CMAKE_SOURCE_DIR}/cpp/tests/shaders/ParticleGpuBillboard.vert"
        "${CMAKE_SOURCE_DIR}/cpp/tests/shaders/ParticleGpuBillboard.frag"
        "${CMAKE_SOURCE_DIR}/cpp/tests/shaders/ParticleGpuTexturedBillboard.vert"
        "${CMAKE_SOURCE_DIR}/cpp/tests/shaders/ParticleGpuTexturedBillboard.frag"
    )
    set(INFERNUX_COMPUTE_INDIRECT_SHADER_OUTPUTS)
    foreach(_shader_source IN LISTS INFERNUX_COMPUTE_INDIRECT_SHADER_SOURCES)
        get_filename_component(_shader_name "${_shader_source}" NAME)
        set(_shader_output "${INFERNUX_COMPUTE_INDIRECT_SHADER_DIR}/${_shader_name}.spv")
        add_custom_command(
            OUTPUT "${_shader_output}"
            COMMAND ${CMAKE_COMMAND} -E make_directory "${INFERNUX_COMPUTE_INDIRECT_SHADER_DIR}"
            COMMAND "${Vulkan_GLSLANG_VALIDATOR_EXECUTABLE}" -V "${_shader_source}" -o "${_shader_output}"
            DEPENDS "${_shader_source}"
            VERBATIM
        )
        list(APPEND INFERNUX_COMPUTE_INDIRECT_SHADER_OUTPUTS "${_shader_output}")
    endforeach()
    add_custom_target(infernux_render_graph_compute_indirect_shaders
        DEPENDS ${INFERNUX_COMPUTE_INDIRECT_SHADER_OUTPUTS}
    )

    add_executable(infernux_render_graph_compute_indirect_tests
        cpp/tests/RenderGraphComputeIndirectTests.cpp
    )
    add_dependencies(infernux_render_graph_compute_indirect_tests infernux_render_graph_compute_indirect_shaders)
    target_include_directories(infernux_render_graph_compute_indirect_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
        ${CMAKE_SOURCE_DIR}/external/VulkanMemoryAllocator/include
    )
    target_link_libraries(infernux_render_graph_compute_indirect_tests PRIVATE infernux_runtime_test_support)
    if(MSVC)
        target_compile_options(infernux_render_graph_compute_indirect_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.render_graph_compute_indirect
        COMMAND infernux_render_graph_compute_indirect_tests
            "${INFERNUX_COMPUTE_INDIRECT_SHADER_DIR}/RenderGraphComputeIndirect.comp.spv"
            "${INFERNUX_COMPUTE_INDIRECT_SHADER_DIR}/RenderGraphComputeIndirect.vert.spv"
            "${INFERNUX_COMPUTE_INDIRECT_SHADER_DIR}/RenderGraphComputeIndirect.frag.spv"
            "${INFERNUX_COMPUTE_INDIRECT_SHADER_DIR}/ShaderReflectionStorage.comp.spv"
            "${INFERNUX_COMPUTE_INDIRECT_SHADER_DIR}/ParticleGpuFixture.comp.spv"
            "${INFERNUX_COMPUTE_INDIRECT_SHADER_DIR}/ParticleGpuBillboard.vert.spv"
            "${INFERNUX_COMPUTE_INDIRECT_SHADER_DIR}/ParticleGpuBillboard.frag.spv"
            "${INFERNUX_COMPUTE_INDIRECT_SHADER_DIR}/ParticleGpuTexturedBillboard.vert.spv"
            "${INFERNUX_COMPUTE_INDIRECT_SHADER_DIR}/ParticleGpuTexturedBillboard.frag.spv"
    )
    set_tests_properties(infernux.render_graph_compute_indirect PROPERTIES
        LABELS "requires_vulkan_device"
        TIMEOUT 180
    )

    add_executable(infernux_renderer_list_tests
        cpp/tests/RendererListTests.cpp
    )
    target_include_directories(infernux_renderer_list_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_renderer_list_tests PRIVATE Vulkan::Vulkan glm::glm)
    if(MSVC)
        target_compile_options(infernux_renderer_list_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.renderer_list COMMAND infernux_renderer_list_tests)

    add_executable(infernux_render_world_tests
        cpp/tests/RenderWorldTests.cpp
    )
    target_include_directories(infernux_render_world_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
        ${CMAKE_SOURCE_DIR}/external/VulkanMemoryAllocator/include
    )
    target_link_libraries(infernux_render_world_tests PRIVATE Vulkan::Vulkan glm::glm)
    if(MSVC)
        target_compile_options(infernux_render_world_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.render_world COMMAND infernux_render_world_tests)

    add_executable(infernux_asset_index_tests
        cpp/tests/AssetIndexTests.cpp
        cpp/infernux/core/log/InxLog.cpp
        cpp/infernux/function/resources/AssetDatabase/AssetIndex.cpp
        cpp/infernux/function/resources/InxResource/InxResourceMeta.cpp
        cpp/infernux/core/reflection/InxTypeRegistry.cpp
        cpp/infernux/platform/filesystem/DocumentStore.cpp
        cpp/infernux/platform/filesystem/AtomicFile.cpp
    )
    target_include_directories(infernux_asset_index_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_asset_index_tests PRIVATE Threads::Threads)
    if(MSVC)
        target_compile_options(infernux_asset_index_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.asset_index COMMAND infernux_asset_index_tests)

    add_executable(infernux_asset_database_refresh_tests
        cpp/tests/AssetDatabaseRefreshTests.cpp
    )
    target_include_directories(infernux_asset_database_refresh_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_asset_database_refresh_tests PRIVATE infernux_runtime_test_support)
    if(MSVC)
        target_compile_options(infernux_asset_database_refresh_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_asset_database_refresh_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.asset_database_refresh COMMAND infernux_asset_database_refresh_tests)

    add_executable(infernux_asset_dependency_graph_tests
        cpp/tests/AssetDependencyGraphTests.cpp
        cpp/infernux/core/log/InxLog.cpp
        cpp/infernux/function/resources/AssetDependencyGraph.cpp
    )
    target_include_directories(infernux_asset_dependency_graph_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_asset_dependency_graph_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.asset_dependency_graph COMMAND infernux_asset_dependency_graph_tests)

    add_executable(infernux_material_document_tests
        cpp/tests/MaterialDocumentTests.cpp
    )
    target_include_directories(infernux_material_document_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_material_document_tests PRIVATE
        infernux_runtime_test_support
    )
    if(MSVC)
        target_compile_options(infernux_material_document_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_material_document_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.material_document COMMAND infernux_material_document_tests)

    add_executable(infernux_material_render_state_vulkan_tests
        cpp/tests/MaterialRenderStateVulkanTests.cpp
    )
    target_include_directories(infernux_material_render_state_vulkan_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_material_render_state_vulkan_tests PRIVATE Vulkan::Vulkan glm::glm)
    target_compile_definitions(infernux_material_render_state_vulkan_tests PRIVATE
        INFERNUX_DISABLE_VULKAN_MATERIAL_RUNTIME=1
    )
    if(MSVC)
        target_compile_options(infernux_material_render_state_vulkan_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_material_render_state_vulkan_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.material_render_state_vulkan COMMAND infernux_material_render_state_vulkan_tests)

    add_executable(infernux_mesh_artifact_tests
        cpp/tests/MeshArtifactTests.cpp
        cpp/infernux/core/log/InxLog.cpp
        cpp/infernux/function/resources/InxMesh/MeshArtifact.cpp
        cpp/infernux/function/resources/InxMesh/InxMesh.cpp
        cpp/infernux/function/resources/InxSkinnedMesh/InxSkinnedMesh.cpp
    )
    target_include_directories(infernux_mesh_artifact_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_mesh_artifact_tests PRIVATE Vulkan::Vulkan glm::glm)
    if(MSVC)
        target_compile_options(infernux_mesh_artifact_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.mesh_artifact COMMAND infernux_mesh_artifact_tests)

    add_executable(infernux_skinned_mesh_artifact_tests
        cpp/tests/SkinnedMeshArtifactTests.cpp
        cpp/infernux/core/log/InxLog.cpp
        cpp/infernux/function/resources/InxSkinnedMesh/SkinnedMeshArtifact.cpp
        cpp/infernux/function/resources/InxSkinnedMesh/InxSkinnedMesh.cpp
    )
    target_include_directories(infernux_skinned_mesh_artifact_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_skinned_mesh_artifact_tests PRIVATE Vulkan::Vulkan glm::glm)
    if(MSVC)
        target_compile_options(infernux_skinned_mesh_artifact_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.skinned_mesh_artifact COMMAND infernux_skinned_mesh_artifact_tests)

    add_executable(infernux_texture_artifact_tests
        cpp/tests/TextureArtifactTests.cpp
        cpp/infernux/function/resources/InxTexture/TextureArtifact.cpp
    )
    target_include_directories(infernux_texture_artifact_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
    )
    if(MSVC)
        target_compile_options(infernux_texture_artifact_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.texture_artifact COMMAND infernux_texture_artifact_tests)

    add_executable(infernux_texture_processor_tests
        cpp/tests/TextureProcessorTests.cpp
        cpp/infernux/function/resources/InxTexture/TextureProcessor.cpp
    )
    target_include_directories(infernux_texture_processor_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external/stb
        ${CMAKE_SOURCE_DIR}/external/glm
    )
    if(MSVC)
        target_compile_options(infernux_texture_processor_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.texture_processor COMMAND infernux_texture_processor_tests)

    add_executable(infernux_vector_field_source_tests
        cpp/tests/VectorFieldSourceTests.cpp
        cpp/infernux/function/resources/InxTexture/VectorFieldSource.cpp
    )
    target_include_directories(infernux_vector_field_source_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    if(MSVC)
        target_compile_options(infernux_vector_field_source_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.vector_field_source COMMAND infernux_vector_field_source_tests)

    add_executable(infernux_signed_distance_field_source_tests
        cpp/tests/SignedDistanceFieldSourceTests.cpp
        cpp/infernux/function/resources/InxTexture/SignedDistanceFieldSource.cpp
    )
    target_include_directories(infernux_signed_distance_field_source_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    if(MSVC)
        target_compile_options(infernux_signed_distance_field_source_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.signed_distance_field_source COMMAND infernux_signed_distance_field_source_tests)

    add_executable(infernux_vector_field_importer_tests
        cpp/tests/VectorFieldImporterTests.cpp
    )
    target_include_directories(infernux_vector_field_importer_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_vector_field_importer_tests PRIVATE infernux_runtime_test_support)
    if(MSVC)
        target_compile_options(infernux_vector_field_importer_tests PRIVATE /utf-8)
    endif()
    add_test(NAME infernux.vector_field_importer COMMAND infernux_vector_field_importer_tests)

    add_executable(infernux_shader_asset_tests
        cpp/tests/ShaderAssetTests.cpp
    )
    target_include_directories(infernux_shader_asset_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_shader_asset_tests PRIVATE infernux_runtime_test_support)
    if(MSVC)
        target_compile_options(infernux_shader_asset_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_shader_asset_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.shader_asset COMMAND infernux_shader_asset_tests)

    add_executable(infernux_shader_info_schema_tests
        cpp/tests/ShaderInfoSchemaTests.cpp
    )
    target_include_directories(infernux_shader_info_schema_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_shader_info_schema_tests PRIVATE infernux_runtime_test_support)
    target_compile_definitions(infernux_shader_info_schema_tests PRIVATE
        INFERNUX_TEST_SHADER_ROOT="${CMAKE_SOURCE_DIR}/python/Infernux/resources/shaders"
    )
    if(MSVC)
        target_compile_options(infernux_shader_info_schema_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_shader_info_schema_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.shader_info_schema COMMAND infernux_shader_info_schema_tests)

    add_executable(infernux_shader_stage_linker_tests
        cpp/tests/ShaderStageLinkerTests.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuRibbonRenderer.cpp
        cpp/infernux/function/renderer/particle/ParticleGpuRibbonTopology.cpp
    )
    target_include_directories(infernux_shader_stage_linker_tests PRIVATE
        ${CMAKE_SOURCE_DIR}/cpp/infernux
        ${CMAKE_SOURCE_DIR}/external
    )
    target_link_libraries(infernux_shader_stage_linker_tests PRIVATE infernux_runtime_test_support)
    target_compile_definitions(infernux_shader_stage_linker_tests PRIVATE
        INFERNUX_TEST_SHADER_ROOT="${CMAKE_SOURCE_DIR}/python/Infernux/resources/shaders"
    )
    if(MSVC)
        target_compile_options(infernux_shader_stage_linker_tests PRIVATE /utf-8 /UNDEBUG)
    else()
        target_compile_options(infernux_shader_stage_linker_tests PRIVATE -UNDEBUG)
    endif()
    add_test(NAME infernux.shader_stage_linker COMMAND infernux_shader_stage_linker_tests)

    add_test(
        NAME infernux.headless
        COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/python/Infernux/test/headless_smoke_test.py
    )

    add_test(
        NAME infernux.bootstrap_import
        COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/python/Infernux/test/bootstrap_import_test.py
    )
    set_tests_properties(infernux.bootstrap_import PROPERTIES
        ENVIRONMENT "INFERNUX_NATIVE_MODULE_DIR=$<TARGET_FILE_DIR:_InfernuxBootstrap>"
    )
    set_tests_properties(infernux.headless PROPERTIES
        ENVIRONMENT "PYTHONPATH=${CMAKE_SOURCE_DIR}/python;INFERNUX_NATIVE_MODULE_DIR=$<TARGET_FILE_DIR:_Infernux>"
        TIMEOUT 60
        RUN_SERIAL TRUE
    )

    # These executables pull substantial portions of the static runtime into
    # separate processes. Serializing their cold starts avoids paging and
    # executable-scanning stalls on small Windows CI runners while leaving the
    # lightweight native tests free to run in parallel.
    set_tests_properties(
        infernux.scene_render_graph_validation
        infernux.py_component_proxy
        infernux.runtime_frame_barrier
        infernux.runtime_persistent_scene
        infernux.camera_culling_isolation
        infernux.particle_gpu_runtime
        infernux.asset_database_refresh
        infernux.material_document
        infernux.vector_field_importer
        infernux.shader_asset
        infernux.shader_info_schema
        infernux.shader_stage_linker
        PROPERTIES RESOURCE_LOCK infernux_runtime_process
    )

    add_test(
        NAME infernux.gpu_upload
        COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/python/Infernux/test/gpu_upload_smoke_test.py
    )
    set_tests_properties(infernux.gpu_upload PROPERTIES
        ENVIRONMENT "PYTHONPATH=${CMAKE_SOURCE_DIR}/python;INFERNUX_NATIVE_MODULE_DIR=$<TARGET_FILE_DIR:_Infernux>"
        LABELS "requires_vulkan_device"
        TIMEOUT 30
        RUN_SERIAL TRUE
        SKIP_RETURN_CODE 77
    )

    add_test(
        NAME infernux.scene_residency_soak
        COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/python/Infernux/test/scene_residency_soak_test.py
    )
    set_tests_properties(infernux.scene_residency_soak PROPERTIES
        ENVIRONMENT "PYTHONPATH=${CMAKE_SOURCE_DIR}/python;INFERNUX_NATIVE_MODULE_DIR=$<TARGET_FILE_DIR:_Infernux>"
        LABELS "requires_vulkan_device"
        TIMEOUT 90
        RUN_SERIAL TRUE
        SKIP_RETURN_CODE 77
    )

    # Performance diagnostics are intentionally opt-in. Their wall-clock
    # thresholds depend on the host CPU and filesystem, so they must not make
    # correctness CI fail on slower remote workers.
    add_custom_target(infernux_performance_diagnostics
        COMMAND ${CMAKE_COMMAND} -E env
                "PYTHONPATH=${CMAKE_SOURCE_DIR}/python"
                "INFERNUX_NATIVE_MODULE_DIR=$<TARGET_FILE_DIR:_Infernux>"
                ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/python/Infernux/test/physics_profile_gate.py
        COMMAND ${CMAKE_COMMAND} -E env
                "PYTHONPATH=${CMAKE_SOURCE_DIR}/python"
                "INFERNUX_NATIVE_MODULE_DIR=$<TARGET_FILE_DIR:_Infernux>"
                ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/python/Infernux/test/asset_scan_profile_gate.py
        DEPENDS _Infernux
        USES_TERMINAL
        VERBATIM
    )
endif()
