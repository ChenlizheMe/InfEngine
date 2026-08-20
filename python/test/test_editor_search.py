from __future__ import annotations


def test_search_query_model_normalizes_and_revisions_only_real_changes():
    from Infernux.engine.interaction.search import SearchQueryModel

    model = SearchQueryModel()
    assert model.revision == 0
    assert not model.active

    assert model.set_query("  Smoke ")
    assert model.query == "  Smoke "
    assert model.normalized_query == "smoke"
    assert model.revision == 1
    assert model.matches("GPU SMOKE Emitter")
    assert not model.matches("Water")

    assert not model.set_query("  Smoke ")
    assert model.revision == 1


def test_search_tokens_reject_stale_query_source_and_scope():
    from Infernux.engine.interaction.search import SearchQueryModel

    model = SearchQueryModel("cube")
    token = model.token(source_generation=7, scope_key="Assets")
    assert model.accepts(token, source_generation=7, scope_key="Assets")
    assert not model.accepts(token, source_generation=8, scope_key="Assets")
    assert not model.accepts(token, source_generation=7, scope_key="Packages")

    model.set_query("sphere")
    assert not model.accepts(token, source_generation=7, scope_key="Assets")


def test_search_clear_is_a_revisioned_transition():
    from Infernux.engine.interaction.search import SearchQueryModel

    model = SearchQueryModel("camera")
    revision = model.revision
    assert model.clear()
    assert model.revision == revision + 1
    assert not model.active
    assert model.matches("anything")
    assert not model.clear()


def test_native_core_panels_use_shared_search_authority():
    from pathlib import Path

    panels = {
        "HierarchyPanel": Path(
            "cpp/infernux/function/editor/HierarchyPanel.h"
        ).read_text(encoding="utf-8"),
        "ProjectPanel": Path(
            "cpp/infernux/function/editor/ProjectPanel.h"
        ).read_text(encoding="utf-8"),
        "ConsolePanel": Path(
            "cpp/infernux/function/editor/ConsolePanel.h"
        ).read_text(encoding="utf-8"),
    }
    for name, source in panels.items():
        assert "EditorSearchModel" in source, name

    combined = "\n".join(panels.values())
    for legacy_member in (
        "m_searchQuery",
        "m_searchQueryNorm",
        "m_lastSearchQuery",
        "m_lastSearchGeneration",
        "m_prevSearch",
    ):
        assert legacy_member not in combined


def test_python_picker_and_node_palette_use_shared_search_authority():
    from pathlib import Path

    object_fields = Path(
        "python/Infernux/engine/interaction/object_fields.py"
    ).read_text(encoding="utf-8")
    node_graph = Path(
        "python/Infernux/engine/ui/node_graph_view.py"
    ).read_text(encoding="utf-8")

    assert "dict[str, SearchQueryModel]" in object_fields
    assert "self._node_create_search = SearchQueryModel()" in node_graph
    assert "_node_create_search: str" not in node_graph
