# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Import a framework at USE time, never at module load time.

One SDK ships every plugin, so a base ``pip install standin-sdk`` puts
LiveKit code and Hermes code on disk for a reader who installed neither. The
rule that keeps that honest is simple and absolute: nothing under
:mod:`standin.plugins` may import its framework while the module is being
loaded. This is the one place that rule is implemented, so a new plugin
inherits it instead of re-deriving it.

Two shapes, because there are two kinds of use:

* :func:`require` for a framework touched once, inside the function that needs
  it.
* :func:`lazy_module` for a framework woven through a whole module
  (``rtc.Room``, ``rtc.AudioFrame``, ``api.AccessToken`` ...). It binds a stand-in
  object that imports the real module on first attribute access, so every call
  site keeps reading exactly as it did.

  A PEP 562 module ``__getattr__`` does NOT work for this, and the trap is worth
  recording: that hook only runs for ``some_module.name`` from OUTSIDE. A plain
  ``rtc.Room()`` inside the module compiles to a global load, which checks the
  module dict and then builtins and never calls the hook - so the name is simply
  undefined and you get a NameError at the first real call, which is the worst
  possible moment to find out.

Either way the failure is :class:`standin.PluginNotInstalled`, which names
the extra to install. A reader who gets ``ModuleNotFoundError: livekit`` out of
somebody else's package cannot tell a missing extra from a broken install.
"""

from __future__ import annotations

import importlib
from typing import Any

from standin._exceptions import PluginNotInstalled

__all__ = ["LazyModule", "lazy_module", "require"]


def require(module: str, *, plugin: str, extra: str | None = None) -> Any:
    """Import ``module``, or raise the error that says what to install."""
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as exc:
        # Only when the framework ITSELF is absent. A framework that is
        # installed but broken raises ModuleNotFoundError for one of its own
        # dependencies, and reporting that as "run pip install" sends the
        # reader to fix something that is already fine.
        top = module.split(".")[0]
        if exc.name is not None and exc.name != top and not exc.name.startswith(top + "."):
            raise
        raise PluginNotInstalled(plugin, top, extra) from exc


class LazyModule:
    """Stands in for a framework module until something actually touches it.

    Bound at module load in place of the real import, so the module loads with
    no framework present; the first attribute access imports for real, and every
    access after that is one extra ``getattr`` on a resolved module.

    Deliberately not a subclass of :class:`types.ModuleType`: a real module
    object resolves attributes out of its own ``__dict__`` before any hook runs,
    which is precisely the behaviour that has to be intercepted here.
    """

    __slots__ = ("_extra", "_plugin", "_name", "_resolved")

    def __init__(self, name: str, *, plugin: str, extra: str | None = None) -> None:
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_plugin", plugin)
        object.__setattr__(self, "_extra", extra)
        object.__setattr__(self, "_resolved", None)

    def _load(self) -> Any:
        module = self._resolved
        if module is None:
            module = require(self._name, plugin=self._plugin, extra=self._extra)
            object.__setattr__(self, "_resolved", module)
        return module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)

    # Writes go through to the real module, so the stand-in is transparent to
    # anything that patches a framework symbol - monkeypatch in the tests, and
    # the occasional runtime shim. Forwarding a write also forces the import,
    # which is right: you cannot patch a module nobody has loaded.
    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._load(), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(self._load(), name)

    def __dir__(self) -> list[str]:
        return dir(self._load())

    def __repr__(self) -> str:
        state = "loaded" if self._resolved is not None else "not loaded yet"
        return f"<lazy module {self._name!r} ({state})>"


def lazy_module(name: str, *, plugin: str, extra: str | None = None) -> Any:
    """Bind ``name`` lazily, for a framework used throughout a module.

    Use it opposite a ``TYPE_CHECKING`` import, so type checkers and IDEs see
    the real module and the interpreter sees this::

        if TYPE_CHECKING:
            from livekit import rtc
        else:
            rtc = lazy_module("livekit.rtc", plugin="livekit", extra="livekit")
    """
    return LazyModule(name, plugin=plugin, extra=extra)
