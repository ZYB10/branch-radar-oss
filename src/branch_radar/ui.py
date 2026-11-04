from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Sequence
from typing import TextIO

from .i18n import t


class Palette:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    BLUE = "\033[34m"


class Console:
    def __init__(
        self, *, color: bool | None = None, stream: TextIO | None = None
    ):
        self.stream = stream or sys.stdout
        if color is None:
            color = bool(getattr(self.stream, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")
        self.color = color

    def _style(self, value: str, *codes: str) -> str:
        if not self.color:
            return value
        return "".join(codes) + value + Palette.RESET

    def print(self, value: str = "") -> None:
        print(value, file=self.stream)

    def header(self, title: str, subtitle: str | None = None) -> None:
        self.print()
        self.print(self._style("  ◉ " + t(title), Palette.BOLD, Palette.CYAN))
        if subtitle:
            self.print(self._style("    " + t(subtitle), Palette.DIM))
        self.print()

    def success(self, message: str) -> None:
        self.print(self._style("  ✓ ", Palette.GREEN) + t(message))

    def info(self, message: str) -> None:
        self.print(self._style("  • ", Palette.BLUE) + t(message))

    def warning(self, message: str) -> None:
        self.print(self._style("  ! ", Palette.YELLOW) + t(message))

    def error(self, message: str) -> None:
        self.print(self._style("  × ", Palette.RED) + t(message))

    def muted(self, message: str) -> None:
        self.print(self._style("    " + t(message), Palette.DIM))

    def table(self, headers: Sequence[str], rows: Iterable[Sequence[object]]) -> None:
        materialized = [[str(cell) for cell in row] for row in rows]
        widths = [_display_width(t(str(header))) for header in headers]
        for row in materialized:
            for index, cell in enumerate(row):
                if index < len(widths):
                    widths[index] = max(widths[index], _display_width(cell))
        header_cells = []
        for index, header in enumerate(headers):
            value = t(str(header))
            padding = widths[index] - _display_width(value)
            header_cells.append(self._style(value + " " * max(padding, 0), Palette.BOLD))
        header_line = "  " + "  ".join(header_cells)
        self.print(header_line)
        self.print("  " + "  ".join("─" * width for width in widths))
        for row in materialized:
            cells: list[str] = []
            for index, cell in enumerate(row):
                padding = widths[index] - _display_width(cell)
                cells.append(cell + " " * max(padding, 0))
            self.print("  " + "  ".join(cells))

    def ask(self, prompt: str, *, default: str | None = None) -> str:
        suffix = self._style(" [%s]" % default, Palette.DIM) if default else ""
        try:
            value = input(
                self._style("  ? ", Palette.CYAN) + t(prompt) + suffix + ": "
            ).strip()
        except EOFError:
            value = ""
        return value or (default or "")

    def confirm(self, prompt: str, *, default: bool = True) -> bool:
        choice = "Y/n" if default else "y/N"
        answer = self.ask("%s (%s)" % (t(prompt), choice)).lower()
        if not answer:
            return default
        return answer in {"y", "yes", "是", "好", "确认"}


def _display_width(value: str) -> int:
    return sum(2 if ord(char) > 0xFF else 1 for char in value)


def parse_selection(value: str, size: int) -> list[int]:
    normalized = value.strip().lower()
    if normalized in {"all", "a", "全部", "*"}:
        return list(range(size))
    if normalized in {"none", "n", "无", ""}:
        return []
    selected = set()
    for part in normalized.replace("，", ",").split(","):
        item = part.strip()
        if not item:
            continue
        if "-" in item:
            left, right = item.split("-", 1)
            start, end = int(left), int(right)
            for number in range(min(start, end), max(start, end) + 1):
                if 1 <= number <= size:
                    selected.add(number - 1)
        else:
            number = int(item)
            if 1 <= number <= size:
                selected.add(number - 1)
    return sorted(selected)
