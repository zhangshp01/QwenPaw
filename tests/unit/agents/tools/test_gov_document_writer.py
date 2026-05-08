# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from qwenpaw.agents.tools.gov_document_writer import (
    _content_block_text,
    gov_document_writer,
)
from qwenpaw.config.context import set_current_workspace_dir


def test_gov_document_writer_requires_content():
    async def _run():
        r = await gov_document_writer(content="")
        text = _content_block_text(r.content[0])
        assert text
        assert "content" in text.lower()

    asyncio.run(_run())


def test_gov_document_writer_writes_file():
    async def _run():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            set_current_workspace_dir(root)
            r = await gov_document_writer(
                title="Holiday Notice",
                content="Body line one.",
                type="notice",
            )
            text = _content_block_text(r.content[0])
            assert text
            assert "govdocs" in text
            md = root / "govdocs" / "Holiday Notice.md"
            assert md.is_file()
            raw = md.read_text(encoding="utf-8")
            assert "Holiday Notice" in raw
            assert "Body line one." in raw

    asyncio.run(_run())
