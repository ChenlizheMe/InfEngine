#pragma once

#include <Python.h>

#include <string>

PyMODINIT_FUNC PyInit__InfernuxWebHost();

[[nodiscard]] bool InfernuxWebFindShaderSource(const std::string &name, const char *stage, std::string &source);
