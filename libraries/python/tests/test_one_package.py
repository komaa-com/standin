# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The guarantees that let ONE package hold every plugin.

Collapsing the SDK and its plugins into a single `standin-sdk` buys one
thing: a capability lands once and every plugin gets it, instead of being
threaded by hand into N wheels. The bill for that is this file.

`import standin` sits above code that references LiveKit and a Hermes host that
Hermes Agent ships itself. A reader who installed neither must not pay for
either, and must never see a bare ModuleNotFoundError out of somebody else's
package. Three rules keep that true, and each one is pinned below:

1. no module under standin/plugins imports its framework while loading;
2. `import standin` imports no plugin at all;
3. a missing framework raises PluginNotInstalled, naming the extra.

Rule 1 is the one that rots silently: a single convenience import at the top of
a new file breaks the base install for everyone, and nothing else in the suite
would notice, because CI has the frameworks installed.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

import standin
from standin import CallServer, CallSession, ChatChannel, FrameAligner
from standin._exceptions import PluginNotInstalled
from standin.plugins._lazy import LazyModule, require

pytestmark = pytest.mark.unit

PKG_DIR = Path(standin.__file__).resolve().parent
PLUGINS_DIR = PKG_DIR / "plugins"

#: Every third-party root a plugin drives. A framework listed here may be
#: imported inside a function, never at module scope. Add to it when you add a
#: plugin, so its framework is covered by the walk below.
FRAMEWORK_ROOTS = {
    "livekit",
    # The Hermes host, which Hermes Agent ships itself.
    # standin/plugins/hermes_agent/api.py is the single module allowed to
    # name these, and it does every one of them inside a function.
    "run_agent",
    "hermes_cli",
    "hermes_constants",
    "agent",
}


def _module_level_imports(path: Path) -> set[str]:
    """Third-party roots this file imports while it is being LOADED.

    Only module scope counts: an import inside a function or a method runs when
    that code runs, which is exactly the deferral the one-package layout needs.
    ``if TYPE_CHECKING:`` does not count either - the interpreter never executes
    it, and it is how these modules keep real type annotations.
    """
    roots: set[str] = set()

    def is_type_checking(test: ast.expr) -> bool:
        if isinstance(test, ast.Name):
            return test.id == "TYPE_CHECKING"
        return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"

    def walk(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
            elif isinstance(node, ast.If):
                if not is_type_checking(node.test):
                    walk(node.body)
                walk(node.orelse)
            elif isinstance(node, ast.Try):
                walk(node.body)
                for handler in node.handlers:
                    walk(handler.body)
                walk(node.orelse)
                walk(node.finalbody)
            elif isinstance(node, ast.With):
                walk(node.body)
            # A FunctionDef or a ClassDef body is deliberately NOT walked.

    walk(ast.parse(path.read_text(encoding="utf-8")).body)
    return roots


def _run(code: str) -> subprocess.CompletedProcess[str]:
    """Run code in a fresh interpreter, from a directory that cannot shadow the
    installed package with the source tree."""
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(Path(sys.executable).parent),
    )


# ------------------------------------------- rule 1: nothing loads a framework


def test_no_plugin_imports_its_framework_at_module_load() -> None:
    """The rule the whole layout rests on, checked file by file.

    CI has livekit-agents installed, so nothing else in this suite would catch a
    stray top-level ``from livekit import rtc``. A base install would, on the
    reader's machine, which is far too late.
    """
    offenders = {
        str(py.relative_to(PKG_DIR)): sorted(_module_level_imports(py) & FRAMEWORK_ROOTS)
        for py in PLUGINS_DIR.rglob("*.py")
        if _module_level_imports(py) & FRAMEWORK_ROOTS
    }
    assert not offenders, (
        "these load a framework at import time, which breaks `import standin` "
        f"on a base install: {offenders}. Use standin.plugins._lazy."
    )


def test_the_core_imports_no_framework_either() -> None:
    """The top of the package is aiohttp and the standard library, full stop."""
    offenders = {
        str(py.relative_to(PKG_DIR)): sorted(_module_level_imports(py) & FRAMEWORK_ROOTS)
        for py in PKG_DIR.glob("*.py")
        if _module_level_imports(py) & FRAMEWORK_ROOTS
    }
    assert not offenders, offenders


# --------------------------------------- rule 2: import standin stays cheap


def test_import_standin_loads_no_plugin() -> None:
    """PEP 562 in standin/__init__.py, proved by what ends up in sys.modules.

    A subprocess because this suite has already imported the plugins by
    the time it runs.
    """
    done = _run(
        "import standin, sys; "
        "print(sorted(m for m in sys.modules "
        "if m.startswith('standin.plugins') or m.split('.')[0] in "
        "{'livekit','run_agent','hermes_cli'}))"
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]", done.stdout


def test_import_standin_survives_a_missing_framework() -> None:
    """The headline promise: no livekit, no Hermes host, and the SDK still works.

    Hermes is the sharp case - its host arrives with Hermes Agent, so no extra
    can ever make it present, and ``import standin`` must not care.
    """
    done = _run(
        "import sys\n"
        "for name in ('livekit', 'livekit.agents', 'run_agent', 'hermes_cli'):\n"
        "    sys.modules[name] = None\n"  # forces ImportError on any import
        "import standin\n"
        "print(standin.CallServer)\n"
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "<class 'standin.call_server.CallServer'>"


def test_the_hermes_plugin_imports_with_no_host() -> None:
    """Hermes ships in the BASE install, so this must hold with nothing added."""
    done = _run(
        "import sys\n"
        "for name in ('run_agent', 'hermes_cli', 'hermes_constants', 'agent'):\n"
        "    sys.modules[name] = None\n"
        "from standin.plugins.hermes_agent import RealtimeHandler\n"
        "print(RealtimeHandler.__name__)\n"
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "RealtimeHandler"


# ------------------------------- rule 3: a missing framework explains itself


def test_a_missing_framework_names_the_extra_to_install() -> None:
    with pytest.raises(PluginNotInstalled) as err:
        require("no_such_framework_at_all", plugin="livekit", extra="livekit")
    message = str(err.value)
    assert 'pip install "standin-sdk[livekit]"' in message
    assert "standin.plugins.livekit" in message


def test_it_is_an_import_error_so_the_usual_probes_still_work() -> None:
    """pytest.importorskip and `except ImportError` must read it as what it is."""
    assert issubclass(PluginNotInstalled, ImportError)
    assert issubclass(PluginNotInstalled, standin.StandInError)


def test_a_framework_that_is_installed_but_broken_is_not_blamed_on_the_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its own missing dependency must propagate, not be reported as ours.

    Telling somebody to `pip install "standin-sdk[livekit]"` when livekit is
    already installed sends them to fix something that is not broken. So the
    error is only claimed when the framework ROOT is what went missing.
    """
    (tmp_path / "framework_present_but_broken.py").write_text(
        "import a_dependency_that_is_not_installed\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(ModuleNotFoundError) as err:
        require("framework_present_but_broken", plugin="livekit", extra="livekit")
    assert not isinstance(err.value, PluginNotInstalled)
    assert err.value.name == "a_dependency_that_is_not_installed"


# ------------------------------------------------------ the lazy module shim


def test_the_lazy_shim_defers_the_import_and_then_disappears() -> None:
    shim = LazyModule("json", plugin="echo")
    assert "not loaded yet" in repr(shim)
    assert shim.dumps({"a": 1}) == '{"a": 1}'
    assert "loaded" in repr(shim)


def test_the_lazy_shim_forwards_writes_so_patching_still_works() -> None:
    """Tests patch framework symbols through the shim; it must be transparent."""
    import json as real_json

    shim = LazyModule("json", plugin="echo")
    original = real_json.dumps
    try:
        shim.dumps = lambda *a, **k: "patched"
        assert real_json.dumps is not original
        assert shim.dumps() == "patched"
    finally:
        real_json.dumps = original


# --------------------------------------------------------- the import surface


def test_the_documented_import_surface() -> None:
    """One line, from the package root, exactly as the README prints it."""
    assert all(x is not None for x in (CallServer, CallSession, ChatChannel, FrameAligner))


def test_every_name_in_all_actually_resolves() -> None:
    missing = [name for name in standin.__all__ if not hasattr(standin, name)]
    assert not missing, missing


def test_the_plugin_shorthands_resolve_lazily() -> None:
    """standin.echo is standin.plugins.echo, imported on first touch."""
    import standin.plugins.echo as echo

    assert standin.echo is echo
    assert standin.plugins.echo is echo


def test_an_unknown_attribute_is_still_an_attribute_error() -> None:
    missing = "no_such_plugin"
    with pytest.raises(AttributeError):
        getattr(standin, missing)
    with pytest.raises(AttributeError):
        getattr(standin.plugins, missing)
