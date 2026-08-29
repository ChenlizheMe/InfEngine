#include <function/renderer/rhi/RhiTypes.h>
#include <function/resources/InxFileLoader/InxShaderLoader.hpp>

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <map>
#include <stdexcept>
#include <vector>

namespace py = pybind11;

namespace infernux
{

void RegisterRhiBindings(py::module_ &m)
{
    m.def(
        "_compile_compute_glsl_batch",
        [](const std::map<std::string, std::string> &sources, const std::string &sourceLabel) {
            // glslang process initialization is global. Reuse one compiler for
            // generated kernels instead of initializing it for every asset save.
            static InxShaderLoader compiler(false, true, false, true, false, true, false, false, false, false);
            py::dict result;
            for (const auto &[stage, source] : sources) {
                const std::string virtualPath = sourceLabel + ":" + stage + ".comp";
                auto spirv = compiler.CompileComputeGlsl(source, virtualPath);
                if (spirv.empty()) {
                    throw std::runtime_error("compute GLSL AOT failed for " + stage + ": " +
                                             InxShaderLoader::GetLastCompileError());
                }
                result[py::str(stage)] = py::bytes(spirv.data(), spirv.size());
            }
            return result;
        },
        py::arg("sources"), py::arg("source_label") = "<generated-compute>",
        "Internal batch compiler for generated compute GLSL");

    m.def(
        "_compile_graphics_glsl_batch",
        [](const std::map<std::string, std::string> &sources, const std::string &sourceLabel) {
            static InxShaderLoader compiler(false, true, false, true, false, true, false, false, false, false);
            py::dict result;
            for (const auto &[stage, source] : sources) {
                std::vector<char> spirv;
                if (stage == "vertex")
                    spirv = compiler.CompileVertexGlsl(source, sourceLabel + ":vertex.vert");
                else if (stage == "fragment")
                    spirv = compiler.CompileFragmentGlsl(source, sourceLabel + ":fragment.frag");
                else
                    throw std::invalid_argument("generated graphics stage must be vertex or fragment");
                if (spirv.empty()) {
                    throw std::runtime_error("graphics GLSL AOT failed for " + stage + ": " +
                                             InxShaderLoader::GetLastCompileError());
                }
                result[py::str(stage)] = py::bytes(spirv.data(), spirv.size());
            }
            return result;
        },
        py::arg("sources"), py::arg("source_label") = "<generated-graphics>",
        "Internal batch compiler for generated graphics GLSL");

    m.def(
        "_prepare_authored_shader_glsl",
        [](const std::string &source, const std::string &sourcePath) {
            static InxShaderLoader compiler(false, true, false, true, false, true, false, false, false, false);
            const auto generated = compiler.PrepareAuthoredStageGlsl(source, sourcePath, ShaderCompileTarget::Forward);
            if (generated.empty())
                throw std::runtime_error("authored shader preprocessing produced no GLSL: " + sourcePath);
            return generated;
        },
        py::arg("source"), py::arg("source_path"), "Internal cross-platform cook entry for authored ShaderInfo source");

    py::enum_<rhi::PixelFormat>(m, "PixelFormat", "Backend-neutral pixel format")
        .value("UNDEFINED", rhi::PixelFormat::Undefined)
        .value("R8_UNORM", rhi::PixelFormat::R8UNorm)
        .value("RG8_UNORM", rhi::PixelFormat::RG8UNorm)
        .value("RGBA8_UNORM", rhi::PixelFormat::RGBA8UNorm)
        .value("RGBA8_SRGB", rhi::PixelFormat::RGBA8Srgb)
        .value("BGRA8_UNORM", rhi::PixelFormat::BGRA8UNorm)
        .value("R16_SFLOAT", rhi::PixelFormat::R16SFloat)
        .value("RG16_SFLOAT", rhi::PixelFormat::RG16SFloat)
        .value("RGBA16_SFLOAT", rhi::PixelFormat::RGBA16SFloat)
        .value("R32_SFLOAT", rhi::PixelFormat::R32SFloat)
        .value("RG32_UINT", rhi::PixelFormat::RG32UInt)
        .value("RGBA32_SFLOAT", rhi::PixelFormat::RGBA32SFloat)
        .value("RGB10A2_UNORM", rhi::PixelFormat::RGB10A2UNorm)
        .value("D32_SFLOAT", rhi::PixelFormat::D32SFloat)
        .value("D24_UNORM_S8_UINT", rhi::PixelFormat::D24UNormS8UInt)
        .def_property_readonly("is_depth", [](rhi::PixelFormat format) { return rhi::IsDepthFormat(format); });

    py::enum_<rhi::SampleCount>(m, "SampleCount", "Backend-neutral MSAA sample count")
        .value("COUNT_1", rhi::SampleCount::One)
        .value("COUNT_2", rhi::SampleCount::Two)
        .value("COUNT_4", rhi::SampleCount::Four)
        .value("COUNT_8", rhi::SampleCount::Eight);
}

} // namespace infernux
