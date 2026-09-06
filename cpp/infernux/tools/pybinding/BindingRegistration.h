#pragma once

#include <pybind11/pybind11.h>

namespace infernux
{

void RegisterInfernuxBindings(pybind11::module_ &module);
void RegisterGUIBindings(pybind11::module_ &module);
void RegisterVector2Bindings(pybind11::module_ &module);
void RegisterVector3Bindings(pybind11::module_ &module);
void RegisterVec4fBindings(pybind11::module_ &module);
void RegisterResourceBindings(pybind11::module_ &module);
void RegisterSceneBindings(pybind11::module_ &module);
void RegisterAssetDatabaseBindings(pybind11::module_ &module);
void RegisterAssetRegistryBindings(pybind11::module_ &module);
void RegisterRhiBindings(pybind11::module_ &module);
void RegisterRenderGraphBindings(pybind11::module_ &module);
void RegisterRenderPipelineBindings(pybind11::module_ &module);
void RegisterCommandBufferBindings(pybind11::module_ &module);
void RegisterTagLayerBindings(pybind11::module_ &module);
void RegisterInputBindings(pybind11::module_ &module);
void RegisterPhysicsBindings(pybind11::module_ &module);
void RegisterAudioBindings(pybind11::module_ &module);
void RegisterBatchBindings(pybind11::module_ &module);

} // namespace infernux
