from types import SimpleNamespace

from Infernux.engine.bootstrap_project import _inxpackage_export_paths
from Infernux.engine.interaction import SelectionSnapshot, SelectionTarget


def _context(paths, payload=None):
    targets = tuple(SelectionTarget.asset(str(path)) for path in paths)
    selection = SelectionSnapshot.create(
        targets,
        owner_id="project",
        primary=targets[0] if targets else None,
    )
    return SimpleNamespace(selection=selection, payload=dict(payload or {}))


def test_file_manager_package_export_uses_the_complete_multi_selection(tmp_path):
    project = tmp_path / "Project"
    first = project / "Assets" / "First"
    second = project / "Assets" / "Second"
    first.mkdir(parents=True)
    second.mkdir(parents=True)

    context = _context(
        (first, second),
        payload={"target_id": str(first)},
    )

    assert _inxpackage_export_paths(context, str(project)) == (
        str(first.resolve()),
        str(second.resolve()),
    )


def test_file_manager_package_export_right_click_outside_selection_uses_target(tmp_path):
    project = tmp_path / "Project"
    selected = project / "Assets" / "Selected"
    target = project / "Assets" / "Target"
    selected.mkdir(parents=True)
    target.mkdir(parents=True)

    context = _context(
        (selected,),
        payload={"target_id": str(target)},
    )

    assert _inxpackage_export_paths(context, str(project)) == (str(target.resolve()),)
