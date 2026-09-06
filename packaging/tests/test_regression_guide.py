"""Keep the contributor workflow map tied to executable tests and CI."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_regression_guide_references_existing_test_modules():
    guide = (ROOT / "TESTING.md").read_text(encoding="utf-8")
    modules = set(re.findall(r"(?:packaging/tests|python/test)/test_\w+\.py", guide))
    assert modules, "The critical-workflow map must name its owning tests"
    for module in sorted(modules):
        assert (ROOT / module).is_file(), f"Update TESTING.md: {module} moved or was removed"


def test_documented_portable_command_matches_ci():
    guide = (ROOT / "TESTING.md").read_text(encoding="utf-8")
    command = guide.split("```sh\n", 1)[1].split("```", 1)[0]
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    portable_job = workflow.split("  portable-hub:\n", 1)[1].split("  windows-desktop:\n", 1)[0]
    pattern = r"packaging/tests/test_\w+\.py"
    assert re.findall(pattern, command) == re.findall(pattern, portable_job)
