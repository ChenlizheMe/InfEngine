#include "InxPythonScriptLoader.hpp"

#include <core/log/InxLog.h>
#include <filesystem>
#include <platform/filesystem/InxPath.h>

namespace infernux
{

InxPythonScriptLoader::InxPythonScriptLoader()
{
    INXLOG_DEBUG("InxPythonScriptLoader initialized");
}

void InxPythonScriptLoader::CreateMeta(const char *content, size_t contentSize, const std::string &filePath,
                                       InxResourceMeta &metaData) const
{
    INXLOG_DEBUG("Creating metadata for Python script: ", filePath);

    // Initialize with Script type
    metaData.Init(content, contentSize, filePath, ResourceType::Script);

    std::filesystem::path path = ToFsPath(filePath);
    std::string extension = FromFsPath(path.extension());
    // Script structure is owned by the Python candidate compiler. Keeping a
    // second regex-based parser here made every source creation and metadata
    // rebuild scan the same file several extra times on the editor thread.
    metaData.AddMetadata("file_type", std::string("script"));
    metaData.AddMetadata("file_extension", extension);
    metaData.AddMetadata("language", std::string("python"));
    metaData.AddMetadata("file_size", contentSize);

    INXLOG_DEBUG("Python script metadata created: ", FromFsPath(path.filename()));
}

} // namespace infernux
