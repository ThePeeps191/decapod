"""Built-in tools."""

from __future__ import annotations

from decapod.tools.base import Tool
from decapod.tools.builtin.code import CODE_TOOLS
from decapod.tools.builtin.drive import DRIVE_TOOLS
from decapod.tools.builtin.email import EMAIL_TOOLS
from decapod.tools.builtin.github import GITHUB_TOOLS
from decapod.tools.builtin.internal import INTERNAL_TOOLS
from decapod.tools.builtin.web import WEB_TOOLS

ALL_TOOLS: list[type[Tool]] = [
    *INTERNAL_TOOLS,
    *DRIVE_TOOLS,
    *WEB_TOOLS,
    *CODE_TOOLS,
    *EMAIL_TOOLS,
    *GITHUB_TOOLS,
]


def builtin_tools() -> list[Tool]:
    return [cls() for cls in ALL_TOOLS]


__all__ = ["ALL_TOOLS", "builtin_tools"]
