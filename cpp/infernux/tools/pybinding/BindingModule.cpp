#include "BindingRegistration.h"

PYBIND11_MODULE(_Infernux, module)
{
#if defined(INFERNUX_PYBIND_WEB_PLAYER)
    module.attr("__runtime_profile__") = "web-player";
    infernux::RegisterVector2Bindings(module);
    infernux::RegisterVector3Bindings(module);
    infernux::RegisterVec4fBindings(module);
    infernux::RegisterBatchBindings(module);
    infernux::RegisterResourceBindings(module);
    infernux::RegisterAssetRegistryBindings(module);
    infernux::RegisterSceneBindings(module);
    infernux::RegisterTagLayerBindings(module);
    infernux::RegisterInputBindings(module);
    infernux::RegisterPhysicsBindings(module);
    infernux::RegisterAudioBindings(module);
#else
    module.attr("__runtime_profile__") = "desktop";
    infernux::RegisterInfernuxBindings(module);
    infernux::RegisterGUIBindings(module);
    infernux::RegisterVector2Bindings(module);
    infernux::RegisterVector3Bindings(module);
    infernux::RegisterVec4fBindings(module);
    infernux::RegisterResourceBindings(module);
    infernux::RegisterAssetDatabaseBindings(module);
    infernux::RegisterAssetRegistryBindings(module);
    infernux::RegisterSceneBindings(module);
    infernux::RegisterTagLayerBindings(module);
    infernux::RegisterRhiBindings(module);
    infernux::RegisterRenderGraphBindings(module);
    infernux::RegisterCommandBufferBindings(module);
    infernux::RegisterRenderPipelineBindings(module);
    infernux::RegisterInputBindings(module);
    infernux::RegisterPhysicsBindings(module);
    infernux::RegisterAudioBindings(module);
    infernux::RegisterBatchBindings(module);
#endif
}
