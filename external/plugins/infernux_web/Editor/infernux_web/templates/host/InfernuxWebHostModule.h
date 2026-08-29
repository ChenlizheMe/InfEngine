#pragma once

#include <Python.h>

#include <string>

namespace infernux::web
{
class WebParticleRuntime;
}

PyMODINIT_FUNC PyInit__InfernuxWebHost();

[[nodiscard]] bool InfernuxWebFindShaderSource(const std::string &name, const char *stage, std::string &source);
void InfernuxWebSetParticleRuntime(infernux::web::WebParticleRuntime *runtime) noexcept;
