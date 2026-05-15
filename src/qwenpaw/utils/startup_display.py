# -*- coding: utf-8 -*-
"""Fancy startup display utilities using rich."""
from __future__ import annotations

import logging
import sys
from typing import Optional, Tuple

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.tree import Tree

_LOG = logging.getLogger("qwenpaw")

_INNER_WIDTH = 44


def _ascii_row(inner_text: str) -> str:
    """One row: | + padded inner (ASCII) + |."""
    inner = inner_text[:_INNER_WIDTH].ljust(_INNER_WIDTH)
    return f"|{inner}|"


def _ascii_banner_lines(
    api_info: Optional[Tuple[str, int]],
    elapsed_seconds: Optional[float],
) -> list[str]:
    """Plain-ASCII summary for logs and redirected stderr."""
    sep = "+" + "-" * _INNER_WIDTH + "+"
    lines = [sep, _ascii_row("  QwenPaw  Status: Ready")]
    if api_info:
        host, port = api_info
        url = f"http://{host}:{port}"
        lines.append(_ascii_row(f"  Address: {url}"))
    if elapsed_seconds is not None:
        lines.append(_ascii_row(f"  Startup: {elapsed_seconds:.3f}s"))
    lines.append(sep)
    return lines


def print_ready_banner(
    api_info: Optional[Tuple[str, int]] = None,
    elapsed_seconds: Optional[float] = None,
) -> None:
    """Show a ready banner: Rich panel on a real TTY; ASCII in logs / pipes.

    Rich box-drawing and symbols are skipped for non-TTY stderr and are
    always mirrored as ASCII via the project logger so ``--log-file`` stays
    readable (and avoids mojibake when editors open logs as legacy code pages).
    """
    ascii_lines = _ascii_banner_lines(api_info, elapsed_seconds)
    for line in ascii_lines:
        _LOG.info(line)

    use_rich = bool(getattr(sys.stderr, "isatty", lambda: False)())
    if not use_rich:
        return

    console = Console(file=sys.stderr)
    console.print()

    if api_info:
        host, port = api_info
        url = f"http://{host}:{port}"

        tree = Tree(
            "[bold green]OK[/bold green] [bold]QwenPaw[/bold]",
            guide_style="bright_black",
        )
        tree.add("[dim]Status:[/dim]  [bold green]Ready[/bold green]")
        tree.add(
            f"[dim]Address:[/dim] [blue underline]{url}[/blue underline]",
        )
        if elapsed_seconds is not None:
            tree.add(
                f"[dim]Startup:[/dim] [yellow]{elapsed_seconds:.3f}s[/yellow]",
            )

        panel = Panel(
            tree,
            border_style="green",
            box=box.ROUNDED,
            padding=(1, 2),
            expand=False,
        )
    else:
        tree = Tree(
            "[bold green]OK[/bold green] [bold]QwenPaw[/bold]",
            guide_style="bright_black",
        )
        tree.add("[dim]Status:[/dim]  [bold green]Ready[/bold green]")
        if elapsed_seconds is not None:
            tree.add(
                f"[dim]Startup:[/dim] [yellow]{elapsed_seconds:.3f}s[/yellow]",
            )

        panel = Panel(
            tree,
            border_style="green",
            box=box.ROUNDED,
            padding=(1, 2),
            expand=False,
        )

    console.print(panel)
    console.print()
