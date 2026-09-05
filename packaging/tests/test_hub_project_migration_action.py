from types import SimpleNamespace

from PySide6.QtWidgets import QApplication, QScrollArea

from i18n import tr
from launcher import GameEngineLauncher
from ui_project_list import ProjectListPane


def test_project_menu_migrates_the_clicked_project_not_previous_selection(tmp_path):
    app = QApplication.instance() or QApplication([])
    records = [
        SimpleNamespace(project_id=str(index), name=f"Project {index}",
                        created_at="", path=str(tmp_path))
        for index in range(2)
    ]
    pane = ProjectListPane(SimpleNamespace(all_projects=lambda: records))
    pane.select_project("0")
    requests = []
    window = SimpleNamespace(
        project_list=pane,
        viewmodel=SimpleNamespace(
            migrate_project=lambda parent: requests.append((pane.get_selected_project_id(), parent)),
        ),
    )
    pane.migrate_requested.connect(lambda project_id: GameEngineLauncher._migrate_project_from_card(window, project_id))
    actions = pane.project_cards["1"]._actions_menu.actions()
    migration = next(action for action in actions if action.text() == tr("Migrate Project"))
    migration.trigger()
    assert requests == [("1", window)]
    pane.close()


def test_long_project_identity_does_not_push_actions_outside_the_viewport(tmp_path):
    app = QApplication.instance() or QApplication([])
    record = SimpleNamespace(
        project_id="long", name="Long project name " * 20, created_at="",
        path=str(tmp_path / ("deep-" * 30)),
    )
    pane = ProjectListPane(SimpleNamespace(all_projects=lambda: [record]))
    pane.resize(700, 300)
    pane.show()
    app.processEvents()
    try:
        scroll = pane.findChild(QScrollArea)
        assert scroll.horizontalScrollBar().maximum() == 0
        button = pane.project_cards["long"]._actions_button
        right = button.mapTo(scroll.viewport(), button.rect().topRight())
        assert scroll.viewport().rect().contains(right)
    finally:
        pane.close()
