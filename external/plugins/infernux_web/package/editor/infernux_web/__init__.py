"""Web build target for Infernux."""

from .doctor import inspect_web_toolchain
from .exporter import WebPlatformExporter
from .shader_pipeline import (
    CompiledWebShader,
    PreparedWebShader,
    WebShaderCompatibilityError,
    WebShaderToolError,
    compile_glsl_to_wgsl,
    prepare_glsl_for_webgpu,
)

__all__ = [
    "CompiledWebShader",
    "PreparedWebShader",
    "WebPlatformExporter",
    "WebShaderCompatibilityError",
    "WebShaderToolError",
    "compile_glsl_to_wgsl",
    "inspect_web_toolchain",
    "prepare_glsl_for_webgpu",
]
