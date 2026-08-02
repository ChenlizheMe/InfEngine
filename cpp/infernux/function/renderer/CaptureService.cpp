#include "CaptureService.h"

#include "vk/VkResourceManager.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <future>
#include <stdexcept>
#include <unordered_map>
#include <utility>
#include <vector>

#define STB_IMAGE_WRITE_IMPLEMENTATION
#include <stb_image_write.h>

namespace infernux
{
namespace
{
constexpr size_t MaxInFlightCaptures = 4;
constexpr size_t MaxRetainedCaptures = 128;

struct EncodeResult
{
    bool ok = false;
    std::string error;
};

float HalfToFloat(uint16_t value)
{
    const uint32_t sign = (value >> 15U) & 0x1U;
    const uint32_t exponent = (value >> 10U) & 0x1FU;
    const uint32_t mantissa = value & 0x3FFU;
    if (exponent == 0U) {
        if (mantissa == 0U)
            return sign ? -0.0F : 0.0F;
        const float result = (static_cast<float>(mantissa) / 1024.0F) * std::pow(2.0F, -14.0F);
        return sign ? -result : result;
    }
    if (exponent == 31U)
        return mantissa ? 0.0F : (sign ? -1.0e30F : 1.0e30F);
    const float result =
        std::pow(2.0F, static_cast<float>(exponent) - 15.0F) * (1.0F + static_cast<float>(mantissa) / 1024.0F);
    return sign ? -result : result;
}

unsigned char DisplayFloatToUnorm8(float value)
{
    // Scene/Game float targets are sampled by ImGui into the UNORM swapchain
    // without an additional transfer function. Match that visible path exactly
    // so engine captures and the editor viewport have identical color values.
    return static_cast<unsigned char>(std::clamp(value, 0.0F, 1.0F) * 255.0F + 0.5F);
}

std::vector<unsigned char> ConvertToRgba8(const vk::ImageReadbackTicket &ticket)
{
    if (ticket.GetChannelCount() != 4 || ticket.GetWidth() == 0 || ticket.GetHeight() == 0)
        throw std::runtime_error("Capture source must be a non-empty four-channel image");

    const size_t pixelCount = static_cast<size_t>(ticket.GetWidth()) * ticket.GetHeight();
    const auto &raw = ticket.GetData();
    std::vector<unsigned char> pixels(pixelCount * 4U);
    const std::string &elementType = ticket.GetElementType();

    if (elementType == "uint8") {
        if (raw.size() != pixels.size())
            throw std::runtime_error("Capture readback byte size does not match RGBA8 dimensions");
        std::copy(raw.begin(), raw.end(), pixels.begin());
        return pixels;
    }

    if (elementType == "float16") {
        if (raw.size() != pixelCount * 8U)
            throw std::runtime_error("Capture readback byte size does not match RGBA16F dimensions");
        const auto *source = reinterpret_cast<const uint16_t *>(raw.data());
        for (size_t i = 0; i < pixelCount; ++i) {
            pixels[i * 4U + 0U] = DisplayFloatToUnorm8(HalfToFloat(source[i * 4U + 0U]));
            pixels[i * 4U + 1U] = DisplayFloatToUnorm8(HalfToFloat(source[i * 4U + 1U]));
            pixels[i * 4U + 2U] = DisplayFloatToUnorm8(HalfToFloat(source[i * 4U + 2U]));
            pixels[i * 4U + 3U] =
                static_cast<unsigned char>(std::clamp(HalfToFloat(source[i * 4U + 3U]), 0.0F, 1.0F) * 255.0F + 0.5F);
        }
        return pixels;
    }

    if (elementType == "float32") {
        if (raw.size() != pixelCount * 16U)
            throw std::runtime_error("Capture readback byte size does not match RGBA32F dimensions");
        const auto *source = reinterpret_cast<const float *>(raw.data());
        for (size_t i = 0; i < pixelCount; ++i) {
            pixels[i * 4U + 0U] = DisplayFloatToUnorm8(source[i * 4U + 0U]);
            pixels[i * 4U + 1U] = DisplayFloatToUnorm8(source[i * 4U + 1U]);
            pixels[i * 4U + 2U] = DisplayFloatToUnorm8(source[i * 4U + 2U]);
            pixels[i * 4U + 3U] =
                static_cast<unsigned char>(std::clamp(source[i * 4U + 3U], 0.0F, 1.0F) * 255.0F + 0.5F);
        }
        return pixels;
    }

    throw std::runtime_error("Capture readback element type is not supported: " + elementType);
}

void AppendPngBytes(void *context, void *data, int size)
{
    auto &bytes = *static_cast<std::vector<unsigned char> *>(context);
    const auto *begin = static_cast<const unsigned char *>(data);
    bytes.insert(bytes.end(), begin, begin + size);
}

EncodeResult EncodePng(const std::shared_ptr<vk::ImageReadbackTicket> &ticket, const std::string &outputPath)
{
    try {
        const auto pixels = ConvertToRgba8(*ticket);
        std::vector<unsigned char> encoded;
        if (stbi_write_png_to_func(AppendPngBytes, &encoded, static_cast<int>(ticket->GetWidth()),
                                   static_cast<int>(ticket->GetHeight()), 4, pixels.data(),
                                   static_cast<int>(ticket->GetWidth() * 4U)) == 0) {
            return {false, "PNG encoder rejected the capture image"};
        }

        const std::filesystem::path path = std::filesystem::u8path(outputPath);
        if (path.has_parent_path())
            std::filesystem::create_directories(path.parent_path());
        std::ofstream stream(path, std::ios::binary | std::ios::trunc);
        if (!stream)
            return {false, "Unable to open capture artifact for writing"};
        stream.write(reinterpret_cast<const char *>(encoded.data()), static_cast<std::streamsize>(encoded.size()));
        if (!stream)
            return {false, "Unable to write the complete capture artifact"};
        return {true, {}};
    } catch (const std::exception &exc) {
        return {false, exc.what()};
    }
}

bool IsTerminal(CaptureStatus status)
{
    return status == CaptureStatus::Completed || status == CaptureStatus::Failed ||
           status == CaptureStatus::Cancelled || status == CaptureStatus::SourceExpired;
}
} // namespace

struct CaptureService::Impl
{
    struct Record
    {
        CaptureSnapshot snapshot;
        std::shared_ptr<vk::ImageReadbackTicket> ticket;
        std::future<EncodeResult> encoder;
        bool cancelRequested = false;
        bool sourceExpired = false;
    };

    uint64_t nextId = 1;
    std::unordered_map<uint64_t, Record> records;
};

const char *CaptureSourceName(CaptureSource source) noexcept
{
    return source == CaptureSource::Scene ? "scene" : "game";
}

const char *CaptureStatusName(CaptureStatus status) noexcept
{
    switch (status) {
    case CaptureStatus::PendingGpu:
        return "pending_gpu";
    case CaptureStatus::PendingEncode:
        return "pending_encode";
    case CaptureStatus::Completed:
        return "completed";
    case CaptureStatus::Failed:
        return "failed";
    case CaptureStatus::Cancelled:
        return "cancelled";
    case CaptureStatus::SourceExpired:
        return "source_expired";
    }
    return "failed";
}

CaptureService::CaptureService() : m_impl(std::make_unique<Impl>())
{
}

CaptureService::~CaptureService() = default;

uint64_t CaptureService::Request(CaptureSource source, const rhi::RenderViewContext &view, uint64_t sourceGeneration,
                                 uint64_t engineFrame, std::string outputPath)
{
    if (outputPath.empty())
        throw std::invalid_argument("Capture output path cannot be empty");
    if (!view.IsValid() || view.kind != rhi::RenderViewKind::Capture ||
        view.output != rhi::RenderOutputKind::Readback || view.source == rhi::InvalidRenderViewId)
        throw std::invalid_argument("Capture requires a valid readback view derived from a source render view");

    size_t inFlight = 0;
    for (const auto &[id, record] : m_impl->records) {
        (void)id;
        if (!IsTerminal(record.snapshot.status))
            ++inFlight;
    }
    if (inFlight >= MaxInFlightCaptures)
        throw std::runtime_error("Capture queue is full");

    if (m_impl->records.size() >= MaxRetainedCaptures) {
        for (auto it = m_impl->records.begin(); it != m_impl->records.end();) {
            if (IsTerminal(it->second.snapshot.status))
                it = m_impl->records.erase(it);
            else
                ++it;
            if (m_impl->records.size() < MaxRetainedCaptures)
                break;
        }
    }

    const uint64_t id = m_impl->nextId++;
    Impl::Record record;
    record.snapshot.id = id;
    record.snapshot.source = source;
    record.snapshot.status = CaptureStatus::PendingGpu;
    record.snapshot.sourceGeneration = sourceGeneration;
    record.snapshot.view = view;
    record.snapshot.engineFrame = engineFrame;
    record.snapshot.width = view.width;
    record.snapshot.height = view.height;
    record.snapshot.outputPath = std::move(outputPath);
    m_impl->records.emplace(id, std::move(record));
    return id;
}

bool CaptureService::AttachReadback(uint64_t captureId, std::shared_ptr<vk::ImageReadbackTicket> ticket,
                                    uint64_t engineFrame)
{
    if (!ticket)
        throw std::invalid_argument("Capture requires a valid GPU readback ticket");
    const auto it = m_impl->records.find(captureId);
    if (it == m_impl->records.end() || IsTerminal(it->second.snapshot.status))
        return false;

    auto &record = it->second;
    if (record.ticket)
        throw std::logic_error("Capture already has an attached GPU readback ticket");
    record.snapshot.engineFrame = engineFrame;
    record.snapshot.width = ticket->GetWidth();
    record.snapshot.height = ticket->GetHeight();
    record.ticket = std::move(ticket);
    return true;
}

void CaptureService::Fail(uint64_t captureId, std::string error)
{
    const auto it = m_impl->records.find(captureId);
    if (it == m_impl->records.end() || IsTerminal(it->second.snapshot.status))
        return;
    it->second.snapshot.status = CaptureStatus::Failed;
    it->second.snapshot.error = std::move(error);
}

CaptureSnapshot CaptureService::Query(uint64_t captureId) const
{
    const auto it = m_impl->records.find(captureId);
    if (it == m_impl->records.end())
        throw std::out_of_range("Capture id was not found");
    return it->second.snapshot;
}

bool CaptureService::Cancel(uint64_t captureId)
{
    const auto it = m_impl->records.find(captureId);
    if (it == m_impl->records.end())
        return false;
    auto &record = it->second;
    if (IsTerminal(record.snapshot.status))
        return false;
    record.cancelRequested = true;
    if (record.snapshot.status == CaptureStatus::PendingGpu) {
        if (record.ticket)
            record.ticket->Cancel();
        record.snapshot.status = CaptureStatus::Cancelled;
    }
    return true;
}

void CaptureService::InvalidateSource(CaptureSource source, uint64_t sourceGeneration)
{
    for (auto &[id, record] : m_impl->records) {
        (void)id;
        if (record.snapshot.source != source || IsTerminal(record.snapshot.status) ||
            record.snapshot.sourceGeneration == sourceGeneration)
            continue;
        record.sourceExpired = true;
        if (record.snapshot.status == CaptureStatus::PendingGpu) {
            record.snapshot.status = CaptureStatus::SourceExpired;
            record.snapshot.error = "Capture source was resized or recreated before completion";
        }
        if (record.ticket && !record.ticket->IsDone())
            record.ticket->Cancel();
    }
}

void CaptureService::Poll()
{
    using namespace std::chrono_literals;
    for (auto &[id, record] : m_impl->records) {
        (void)id;
        if (record.snapshot.status == CaptureStatus::PendingGpu && record.ticket && record.ticket->IsDone()) {
            const auto status = record.ticket->GetStatus();
            if (status == vk::ImageReadbackStatus::Completed) {
                record.snapshot.status = CaptureStatus::PendingEncode;
                const auto ticket = record.ticket;
                const auto path = record.snapshot.outputPath;
                record.encoder = std::async(std::launch::async, [ticket, path]() { return EncodePng(ticket, path); });
            } else if (status == vk::ImageReadbackStatus::Cancelled) {
                record.snapshot.status = CaptureStatus::Cancelled;
            } else {
                record.snapshot.status = CaptureStatus::Failed;
                record.snapshot.error = record.ticket->GetError();
            }
        }

        if (record.snapshot.status == CaptureStatus::PendingEncode && record.encoder.valid() &&
            record.encoder.wait_for(0ms) == std::future_status::ready) {
            const EncodeResult result = record.encoder.get();
            if (record.sourceExpired) {
                std::error_code ignored;
                std::filesystem::remove(std::filesystem::u8path(record.snapshot.outputPath), ignored);
                record.snapshot.status = CaptureStatus::SourceExpired;
                record.snapshot.error = "Capture source was resized or recreated before completion";
            } else if (record.cancelRequested) {
                std::error_code ignored;
                std::filesystem::remove(std::filesystem::u8path(record.snapshot.outputPath), ignored);
                record.snapshot.status = CaptureStatus::Cancelled;
            } else if (result.ok) {
                record.snapshot.status = CaptureStatus::Completed;
            } else {
                record.snapshot.status = CaptureStatus::Failed;
                record.snapshot.error = result.error;
            }
        }
    }
}

} // namespace infernux
