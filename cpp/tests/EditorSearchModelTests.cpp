#include <function/editor/interaction/EditorSearchModel.h>

#include <cassert>

int main()
{
    using infernux::EditorSearchModel;

    EditorSearchModel search;
    assert(search.Revision() == 0);
    assert(!search.IsActive());
    assert(search.SetQuery("  Smoke "));
    assert(search.Query() == "  Smoke ");
    assert(search.NormalizedQuery() == "smoke");
    assert(search.Matches("GPU SMOKE Emitter"));
    assert(!search.Matches("Water"));
    assert(!search.SetQuery("  Smoke "));

    const auto token = search.MakeToken(7, "Assets");
    assert(search.Accepts(token, 7, "Assets"));
    assert(!search.Accepts(token, 8, "Assets"));
    assert(!search.Accepts(token, 7, "Packages"));

    assert(search.SetQuery("sphere"));
    assert(!search.Accepts(token, 7, "Assets"));
    assert(search.Clear());
    assert(!search.IsActive());
    assert(search.Matches("anything"));
    assert(!search.Clear());
    return 0;
}
