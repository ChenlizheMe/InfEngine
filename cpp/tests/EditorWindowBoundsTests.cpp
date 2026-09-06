#include <function/renderer/gui/EditorWindowBounds.h>

#include <cassert>

static void RenderWindow(const char *name)
{
    infernux::ConstrainNextFloatingWindowToMainViewport(name, 0);
    ImGui::Begin(name);
    ImGui::TextUnformatted("Build controls");
    ImGui::End();
}

int main()
{
    ImGui::CreateContext();
    auto &io = ImGui::GetIO();
    io.IniFilename = nullptr;
    io.DisplaySize = ImVec2(958, 699);
    io.DeltaTime = 1.0f / 60.0f;
    io.ConfigFlags |= ImGuiConfigFlags_DockingEnable;
    io.ConfigDockingAlwaysTabBar = true;
    unsigned char *pixels;
    int width, height;
    io.Fonts->GetTexDataAsRGBA32(&pixels, &width, &height);

    for (int frame = 0; frame < 6; ++frame) {
        ImGui::NewFrame();
        if (frame == 0) {
            ImGui::SetNextWindowPos(ImVec2(60, 60));
            ImGui::SetNextWindowSize(ImVec2(980, 720));
        }
        RenderWindow("Build###build_settings");
        ImGui::Render();
    }
    auto *window = ImGui::FindWindowByName("Build###build_settings");
    assert(window && window->DockNode);
    auto *root = ImGui::DockNodeGetRootNode(window->DockNode);
    assert(root->IsFloatingNode());
    assert(root->Pos.x >= 0 && root->Pos.y >= 0);
    assert(root->Pos.x + root->Size.x <= io.DisplaySize.x);
    assert(root->Pos.y + root->Size.y <= io.DisplaySize.y);
    assert(window->Pos.y + window->Size.y <= io.DisplaySize.y);

    // Resizing the application also constrains an existing floating tab group.
    const ImGuiID floatingId = root->ID;
    io.DisplaySize = ImVec2(720, 500);
    for (int frame = 0; frame < 4; ++frame) {
        ImGui::NewFrame();
        RenderWindow("Build###build_settings");
        ImGui::Render();
    }
    assert(ImGui::DockNodeGetRootNode(window->DockNode)->ID == floatingId);
    assert(root->Pos.x + root->Size.x <= 720);
    assert(root->Pos.y + root->Size.y <= 500);

    // Multiple floating tabs/splits must keep their shared root and topology.
    ImGui::NewFrame();
    const ImGuiID group = ImGui::GetID("floating-split");
    ImGui::DockBuilderAddNode(group);
    ImGui::DockBuilderSetNodePos(group, ImVec2(100, 100));
    ImGui::DockBuilderSetNodeSize(group, ImVec2(1100, 800));
    ImGuiID left, right;
    ImGui::DockBuilderSplitNode(group, ImGuiDir_Left, 0.5f, &left, &right);
    ImGui::DockBuilderDockWindow("Left", left);
    ImGui::DockBuilderDockWindow("Right", right);
    ImGui::DockBuilderFinish(group);
    RenderWindow("Left");
    RenderWindow("Right");
    ImGui::Render();
    for (int frame = 0; frame < 4; ++frame) {
        ImGui::NewFrame();
        RenderWindow("Left");
        RenderWindow("Right");
        ImGui::Render();
    }
    auto *split = ImGui::DockBuilderGetNode(group);
    assert(split && split->IsFloatingNode());
    assert(split->ChildNodes[0]->ID == left && split->ChildNodes[1]->ID == right);
    assert(split->Pos.x + split->Size.x <= 720);
    assert(split->Pos.y + split->Size.y <= 500);
    for (const char *name : {"Left", "Right"}) {
        auto *child = ImGui::FindWindowByName(name);
        assert(child->Pos.x + child->Size.x <= 720);
        assert(child->Pos.y + child->Size.y <= 500);
    }

    // A dockspace is owned by its host layout, never by an individual panel.
    ImGui::NewFrame();
    const ImGuiID dockspace = ImGui::GetID("owned-dockspace");
    ImGui::DockBuilderAddNode(dockspace, ImGuiDockNodeFlags_DockSpace);
    ImGui::DockBuilderSetNodePos(dockspace, ImVec2(30, 40));
    ImGui::DockBuilderSetNodeSize(dockspace, ImVec2(1200, 1000));
    ImGui::DockBuilderDockWindow("Docked", dockspace);
    ImGui::DockBuilderFinish(dockspace);
    RenderWindow("Docked");
    infernux::ConstrainNextFloatingWindowToMainViewport("Docked", 0);
    auto *owned = ImGui::DockBuilderGetNode(dockspace);
    assert(owned->Pos.x == 30 && owned->Pos.y == 40);
    assert(owned->Size.x == 1200 && owned->Size.y == 1000);
    ImGui::Render();
    ImGui::DestroyContext();
}
