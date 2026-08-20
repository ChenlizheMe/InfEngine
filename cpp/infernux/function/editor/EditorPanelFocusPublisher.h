#pragma once

#include <functional>

namespace infernux
{

/// Bridges immediate-mode focus state to the global editor FocusService.
///
/// Native panels can render before Python finishes wiring their callbacks.
/// Publishing only on a focus transition would then permanently lose an
/// initially-focused panel.  This helper publishes the current state once a
/// subscriber first exists, followed by ordinary transition-only updates.
class EditorPanelFocusPublisher
{
  public:
    void Publish(bool focused, const std::function<void(bool, bool)> &callback, bool userActivated = false,
                 bool force = false)
    {
        m_focused = focused;
        if (!callback)
            return;
        if (!force && m_published && focused == m_publishedFocus)
            return;
        m_published = true;
        m_publishedFocus = focused;
        callback(focused, userActivated);
    }

    void Republish(const std::function<void(bool, bool)> &callback)
    {
        m_published = false;
        Publish(m_focused, callback, false);
    }

    bool IsFocused() const
    {
        return m_focused;
    }

    void RepublishCurrent(const std::function<void(bool, bool)> &callback)
    {
        Publish(m_focused, callback, false, true);
    }

  private:
    bool m_focused = false;
    bool m_published = false;
    bool m_publishedFocus = false;
};

} // namespace infernux
