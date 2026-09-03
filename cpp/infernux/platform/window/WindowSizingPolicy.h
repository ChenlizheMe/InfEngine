#pragma once

#include <algorithm>
#include <stdexcept>

namespace infernux
{

struct WindowSize
{
    int width = 0;
    int height = 0;
};

inline WindowSize ResolveEditorInitialWindowSize(int requestedWidth, int requestedHeight, int usableWidth,
                                                 int usableHeight)
{
    if (requestedWidth <= 0 || requestedHeight <= 0)
        throw std::invalid_argument("Editor window size must be positive");
    if (usableWidth <= 0 || usableHeight <= 0)
        throw std::invalid_argument("Primary display usable bounds must be positive");

    // Leave room for window-manager decorations before the asynchronous
    // maximize request is committed. This prevents a large requested client
    // area from being centered beyond a smaller display's visible bounds.
    const int decoratedWidth = std::max(1, usableWidth * 9 / 10);
    const int decoratedHeight = std::max(1, usableHeight * 9 / 10);
    return {std::min(requestedWidth, decoratedWidth), std::min(requestedHeight, decoratedHeight)};
}

} // namespace infernux
