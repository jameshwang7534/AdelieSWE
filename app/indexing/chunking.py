"""Deterministic line-bounded chunks, preferring Python top-level definitions and blank lines."""

import ast
import hashlib
from dataclasses import dataclass

from app.indexing.scanner import SourceFile


@dataclass(frozen=True)
class Chunk:
    file_path: str
    language: str
    start_line: int
    end_line: int
    content: str
    content_hash: str


def chunk_source(source: SourceFile, max_lines: int = 120, max_chars: int = 8000) -> list[Chunk]:
    if max_lines < 1 or max_chars < 1:
        raise ValueError("invalid_chunk_limits")
    lines = source.content.splitlines(keepends=True)
    if any(len(line) > max_chars for line in lines):
        raise ValueError("source_line_exceeds_chunk_limit")
    boundaries: set[int] = set()
    if source.language == "python":
        try:
            tree = ast.parse(source.content)
        except SyntaxError:
            tree = None  # Incomplete source still receives deterministic line chunks.
        if tree:
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    start = min([node.lineno, *(item.lineno for item in node.decorator_list)])
                    boundaries.add(start - 1)
    chunks: list[Chunk] = []
    start = 0
    while start < len(lines):
        end, size = start, 0
        while end < len(lines) and end - start < max_lines and size + len(lines[end]) <= max_chars:
            size += len(lines[end])
            end += 1
        if end < len(lines):
            minimum = start + max(1, (end - start) // 2)
            preferred = [position for position in boundaries if minimum <= position <= end]
            if not preferred:
                preferred = [
                    position
                    for position in range(minimum, end + 1)
                    if not lines[position - 1].strip()
                ]
            if preferred:
                end = max(preferred)
        content = "".join(lines[start:end])
        chunks.append(
            Chunk(
                source.path,
                source.language,
                start + 1,
                end,
                content,
                hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
        )
        start = end
    return chunks
