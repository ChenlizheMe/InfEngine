#include <platform/input/InputManager.h>

#include <SDL3/SDL.h>
#include <cassert>

using infernux::InputManager;

namespace
{
SDL_Event KeyEvent(SDL_EventType type, SDL_Scancode scancode)
{
    SDL_Event event{};
    event.type = type;
    event.key.scancode = scancode;
    return event;
}

SDL_Event MouseButtonEvent(SDL_EventType type, Uint8 button)
{
    SDL_Event event{};
    event.type = type;
    event.button.button = button;
    return event;
}
} // namespace

int main()
{
    auto &input = InputManager::Instance();
    input.ResetAll();

    input.BeginFrame();
    auto keyDown = KeyEvent(SDL_EVENT_KEY_DOWN, SDL_SCANCODE_W);
    auto keyUp = KeyEvent(SDL_EVENT_KEY_UP, SDL_SCANCODE_W);
    input.ProcessSDLEvent(keyDown);
    input.ProcessSDLEvent(keyUp);
    assert(input.GetKeyDown(SDL_SCANCODE_W));
    assert(input.GetKeyUp(SDL_SCANCODE_W));
    assert(!input.GetKey(SDL_SCANCODE_W));
    assert(input.AnyKeyDown());

    input.BeginFrame();
    assert(!input.GetKeyDown(SDL_SCANCODE_W));
    assert(!input.GetKeyUp(SDL_SCANCODE_W));

    input.ProcessSDLEvent(keyDown);
    input.ProcessSDLEvent(keyDown);
    assert(input.GetKeyDown(SDL_SCANCODE_W));
    assert(input.GetKey(SDL_SCANCODE_W));
    input.BeginFrame();
    input.ProcessSDLEvent(keyDown);
    assert(!input.GetKeyDown(SDL_SCANCODE_W));
    input.ProcessSDLEvent(keyUp);
    assert(input.GetKeyUp(SDL_SCANCODE_W));

    input.BeginFrame();
    auto mouseDown = MouseButtonEvent(SDL_EVENT_MOUSE_BUTTON_DOWN, SDL_BUTTON_LEFT);
    auto mouseUp = MouseButtonEvent(SDL_EVENT_MOUSE_BUTTON_UP, SDL_BUTTON_LEFT);
    input.ProcessSDLEvent(mouseDown);
    input.ProcessSDLEvent(mouseUp);
    assert(input.GetMouseButtonDown(0));
    assert(input.GetMouseButtonUp(0));
    assert(!input.GetMouseButton(0));

    input.ResetAll();
    return 0;
}
