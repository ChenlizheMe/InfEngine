from __future__ import annotations

import textwrap

import pytest

from Infernux.engine.script_candidate_policy import analyze_script_candidate


def _report(source: str):
    return analyze_script_candidate(textwrap.dedent(source).encode("utf-8"), filename="candidate.py")


@pytest.mark.parametrize(
    "source",
    (
        "from Infernux import *\n",
        "from Infernux.components import *\n",
        "import numpy as np\nDEFAULT = np.array([1, 2, 3])\n",
        "import numpy as np\nDEFAULT = np.zeros((2, 3), dtype=np.float32)\n",
        "from numpy import dtype, asarray\nDEFAULT = asarray([1, 2], dtype=dtype('f4'))\n",
        "from Infernux import Vector3, serialized_field\nVALUE = Vector3(1, 2, 3)\nfield = serialized_field(default=1.0)\n",
        "from dataclasses import dataclass\n@dataclass\nclass Config:\n    value: int = 1\n",
        "from pathlib import Path\nROOT = Path('Assets')\n",
        "def update(self, delta_time):\n    open('out.txt', 'w')\n    import subprocess\n    subprocess.run(['tool'])\n",
        "class Component:\n    def update(self):\n        import os\n        os.environ['X'] = '1'\n",
    ),
)
def test_common_imports_declarations_and_function_bodies_are_not_blocked(source):
    report = _report(source)

    assert report.blocked == ()


@pytest.mark.parametrize(
    "source",
    (
        "from Infernux.jit import njit\n@njit(auto_parallel=True)\ndef kernel(values):\n    return values\n",
        "from Infernux import njit\n@njit(cache=True)\ndef kernel(values):\n    return values\n",
        "from Infernux import *\n@njit(auto_parallel=True, parallel_policy='auto')\ndef kernel(values):\n    return values\n",
        "from Infernux.jit import njit as compile_kernel\n@compile_kernel(auto_parallel=True)\ndef kernel(values):\n    return values\n",
    ),
)
def test_public_njit_is_allowed_only_as_a_controlled_declaration_decorator(source):
    report = _report(source)

    assert report.blocked == ()
    assert report.runtime_guard_required == ()


def test_public_njit_name_is_not_a_general_top_level_call_capability():
    report = _report(
        "from Infernux.jit import njit\nfactory = njit(auto_parallel=True)\n"
    )

    assert report.blocked == ()
    assert len(report.runtime_guard_required) == 1
    assert report.runtime_guard_required[0].operation == "Infernux.jit.njit"


def test_render_effect_feature_is_allowed_only_as_a_declaration_decorator():
    report = _report(
        "from Infernux.renderstack import render_effect_feature\n"
        "@render_effect_feature('tests.post.effect')\n"
        "class Effect:\n"
        "    pass\n"
    )

    assert report.blocked == ()
    assert report.runtime_guard_required == ()

    eager = _report(
        "from Infernux.renderstack import render_effect_feature\n"
        "factory = render_effect_feature('tests.post.effect')\n"
    )
    assert len(eager.runtime_guard_required) == 1


@pytest.mark.parametrize(
    ("source", "code", "operation"),
    (
        ("import helper\nhelper.VALUE = 2\n", "NX-R1-STATIC-MODULE-WRITE", "helper.VALUE"),
        ("from helper import VALUE as current\ncurrent += 1\n", "NX-R1-STATIC-MODULE-WRITE", "current"),
        ("import helper\ndel helper.VALUE\n", "NX-R1-STATIC-MODULE-WRITE", "helper.VALUE"),
        ("import os\nos.environ['MODE'] = 'x'\n", "NX-R1-STATIC-ENVIRONMENT-WRITE", "os.environ"),
        ("from os import environ as env\nenv['MODE'] = 'x'\n", "NX-R1-STATIC-ENVIRONMENT-WRITE", "env"),
        ("import sys\nsys.path.append('Assets')\n", "NX-R1-STATIC-MODULE-WRITE", "sys.path.append"),
        ("import sys\nsys.modules['helper'] = None\n", "NX-R1-STATIC-MODULE-WRITE", "sys.modules"),
        ("import sys\nsys.meta_path.clear()\n", "NX-R1-STATIC-MODULE-WRITE", "sys.meta_path.clear"),
        ("open('out.txt', 'wb')\n", "NX-R1-STATIC-FILE-WRITE", "open"),
        ("from io import open as write\nwrite('out.txt', mode='a')\n", "NX-R1-STATIC-FILE-WRITE", "open"),
        ("from pathlib import Path\nPath('out.txt').write_text('x')\n", "NX-R1-STATIC-FILE-WRITE", "Path.write_text"),
        ("from pathlib import Path\nPath('out.txt').write_custom('x')\n", "NX-R1-STATIC-FILE-WRITE", "Path.write_custom"),
        ("from pathlib import Path\nPath('out.txt').touch()\n", "NX-R1-STATIC-FILE-WRITE", "Path.touch"),
        ("from pathlib import Path\np = Path('out.txt')\np.unlink()\n", "NX-R1-STATIC-FILE-WRITE", "p.unlink"),
        ("import os\nos.remove('out.txt')\n", "NX-R1-STATIC-FILE-WRITE", "os.remove"),
        ("import os\nos.system('tool')\n", "NX-R1-STATIC-PROCESS", "os.system"),
        ("import os\nos.spawnv(os.P_WAIT, 'tool', ())\n", "NX-R1-STATIC-PROCESS", "os.spawnv"),
        ("import subprocess\nsubprocess.run(['tool'])\n", "NX-R1-STATIC-PROCESS", "subprocess.run"),
        ("from threading import Thread as Worker\nWorker(target=lambda: None)\n", "NX-R1-STATIC-PROCESS", "threading.Thread"),
        ("import socket\nsocket.socket()\n", "NX-R1-STATIC-PROCESS", "socket.socket"),
        ("import atexit\natexit.register(lambda: None)\n", "NX-R1-STATIC-PROCESS", "atexit.register"),
        ("from importlib import reload as reload_module\nreload_module(helper)\n", "NX-R1-STATIC-DYNAMIC-CODE", "importlib.reload"),
        ("exec('value = 1')\n", "NX-R1-STATIC-DYNAMIC-CODE", "exec"),
    ),
)
def test_obvious_top_level_side_effects_are_blocked(source, code, operation):
    report = _report(source)

    assert report.is_blocked
    assert report.blocked[0].code == code
    assert report.blocked[0].operation == operation
    assert report.blocked[0].line >= 1
    assert report.blocked[0].column >= 0


def test_unknown_top_level_calls_are_runtime_guard_requirements_not_rejections():
    report = _report(
        """
        from Infernux.components import InxComponent
        result = user_factory(1)
        class Probe(InxComponent):
            pass
        """
    )

    assert report.blocked == ()
    assert len(report.runtime_guard_required) == 1
    assert report.runtime_guard_required[0].code == "NX-R1-RUNTIME-GUARD-CALL"
    assert report.runtime_guard_required[0].operation == "user_factory"
    assert report.is_rejected
    assert "cannot be proven isolated" in report.runtime_guard_required[0].message


@pytest.mark.parametrize(
    "source",
    (
        "import importlib\nmodule = importlib.import_module('helper')\n",
        "target = object()\nsetattr(target, 'value', 1)\n",
        "target = object()\ndelattr(target, 'value')\n",
        "import numpy as np\nnp.save('state.npy', np.zeros(1))\n",
        "import numpy as np\nnp.seterr(all='ignore')\n",
        "from numpy import save\nsave('state.npy', [1])\n",
    ),
)
def test_unknown_dynamic_and_numpy_mutating_calls_fail_closed(source):
    report = _report(source)

    assert report.blocked == ()
    assert report.requires_runtime_guard
    assert report.is_rejected
    assert all(
        "cannot be proven isolated" in issue.message
        for issue in report.runtime_guard_required
    )


def test_numpy_module_is_not_whitelisted_as_a_whole():
    report = _report(
        """
        import numpy as np
        a = np.array([1, 2, 3])
        b = np.zeros(3, dtype=np.float32)
        np.seterr(invalid='ignore')
        np.save('state.npy', b)
        """
    )

    assert [issue.operation for issue in report.runtime_guard_required] == [
        "numpy.seterr",
        "numpy.save",
    ]


def test_report_is_immutable_and_preserves_source_locations():
    report = _report("import helper\nhelper.VALUE = 2\n")

    with pytest.raises(AttributeError):
        report.blocked = ()
    assert report.blocked[0].line == 2
    assert report.blocked[0].column == 0
