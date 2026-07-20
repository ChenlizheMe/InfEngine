#include <platform/filesystem/InxPath.h>

#include <cassert>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <string>

namespace
{
std::filesystem::path UniqueTestRoot()
{
    const auto nonce = std::chrono::steady_clock::now().time_since_epoch().count();
    return std::filesystem::temp_directory_path() / ("infernux-path-" + std::to_string(nonce));
}
} // namespace

int main()
{
    using namespace infernux;

    const auto root = UniqueTestRoot();
    const auto nested = root / ToFsPath("Unicode-路径") / "nested";
    const auto file = nested / "asset.txt";
    const auto sibling = root.parent_path() / (FromFsPath(root.filename()) + "-sibling") / "asset.txt";
    std::filesystem::create_directories(nested);
    std::ofstream(file) << "path";

    const std::string rootString = FromFsPath(root);
    const std::string fileString = FromFsPath(file);
    assert(!FilesystemPathsEquivalent("", ""));
    assert(FilesystemPathsEquivalent(fileString, FromFsPath(nested / "." / "asset.txt")));
    assert(IsFilesystemPathWithin(fileString, rootString));
    assert(IsFilesystemPathWithin(rootString, rootString));
    assert(!IsFilesystemPathWithin(rootString, rootString, false));
    assert(!IsFilesystemPathWithin(FromFsPath(sibling), rootString));

    std::string relative;
    assert(TryMakeRelativeFilesystemPath(fileString, rootString, relative));
    assert(relative == NormalizePortablePath(FromFsPath(ToFsPath("Unicode-路径") / "nested" / "asset.txt")));
    assert(!TryMakeRelativeFilesystemPath(FromFsPath(sibling), rootString, relative));
    assert(NormalizePortablePath("Assets\\Textures\\test.png") == "Assets/Textures/test.png");
    assert(TryNormalizePortableRelativePath("Assets/Textures/../Materials/test.mat", relative));
    assert(relative == "Assets/Materials/test.mat");
    assert(!TryNormalizePortableRelativePath("../outside.txt", relative));
    assert(!TryNormalizePortableRelativePath(FromFsPath(root), relative));

    const auto missing = nested / "deleted.txt";
    const std::string missingKey = LexicalFilesystemPathKey(FromFsPath(missing));
    std::ofstream(missing) << "temporary";
    std::filesystem::remove(missing);
    assert(LexicalFilesystemPathKey(FromFsPath(missing)) == missingKey);
    assert(IsFilesystemPathWithin(FromFsPath(missing), rootString));

    const auto hardLink = nested / "asset-hardlink.txt";
    std::error_code hardLinkError;
    std::filesystem::create_hard_link(file, hardLink, hardLinkError);
    if (!hardLinkError)
        assert(FilesystemPathsEquivalent(fileString, FromFsPath(hardLink)));

    std::filesystem::remove_all(root);
    return 0;
}
