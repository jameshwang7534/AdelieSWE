"""Reusable fixture exercises source selection and exact chunk reconstruction."""

import socket
from pathlib import Path

import pytest

from app.indexing.chunking import chunk_source
from app.indexing.scanner import RepositoryScanner


def test_fixture_chunk_roundtrip(source_repository: Path) -> None:
    sources = list(RepositoryScanner().scan(source_repository))
    assert {source.path for source in sources} == {"src/users.py", "src/client.ts", "README.md"}
    for source in sources:
        chunks = chunk_source(source, max_lines=2, max_chars=100)
        assert "".join(chunk.content for chunk in chunks) == source.content
        assert chunks == chunk_source(source, max_lines=2, max_chars=100)
        assert chunks[0].start_line == 1
        assert chunks[-1].end_line == len(source.content.splitlines())


def test_external_network_blocked() -> None:
    with pytest.raises(AssertionError, match="External network disabled"):
        socket.getaddrinfo("api.github.com", 443)
