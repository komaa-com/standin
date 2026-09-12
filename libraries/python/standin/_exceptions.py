# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

from __future__ import annotations


class StandInError(Exception):
    """Raised for StandIn configuration and protocol errors."""


class PluginNotInstalled(StandInError, ImportError):
    """A framework a plugin drives is not installed.

    One SDK ships every plugin, so ``import standin`` pulls in code that
    references frameworks the reader may never install. That must never be a
    bare ``ModuleNotFoundError`` three frames deep in somebody else's package:
    the reader has no way to tell a missing extra from a broken install.

    It also subclasses :class:`ImportError` on purpose, so the ordinary probes
    keep working - ``except ImportError``, ``importlib.util.find_spec`` callers,
    and ``pytest.importorskip`` all treat a missing extra as a missing import,
    which is exactly what it is.
    """

    def __init__(self, plugin: str, module: str, extra: str | None = None) -> None:
        install = f'pip install "standin-sdk[{extra}]"' if extra else None
        detail = f"install it with:\n\n    {install}\n" if install else "install it first."
        super().__init__(
            f"standin.plugins.{plugin} needs {module!r}, which is not installed. "
            f"The base standin-sdk install is deliberately dependency-light, so {detail}"
        )
        self.plugin = plugin
        self.module = module
        self.extra = extra
