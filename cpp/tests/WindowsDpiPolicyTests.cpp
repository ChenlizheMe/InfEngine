#include <platform/window/WindowsDpiPolicy.h>

#include <SDL3/SDL.h>

#include <exception>
#include <iostream>

int main()
{
    try {
        infernux::ConfigureRequiredWindowsDpiPolicy();
        if (!SDL_Init(SDL_INIT_VIDEO)) {
            std::cerr << "SDL video initialization failed: " << SDL_GetError() << '\n';
            return 1;
        }
        infernux::VerifyRequiredWindowsDpiPolicy();
        SDL_Quit();
        return 0;
    } catch (const std::exception &error) {
        SDL_Quit();
        std::cerr << error.what() << '\n';
        return 2;
    }
}
