"""Bounded UTF-8 source scanning with conservative exclusions; no external services."""

import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".c": "c",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".sql": "sql",
    ".sh": "shell",
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
}
EXCLUDED_DIRECTORIES = {
    ".git",
    "node_modules",
    "dist",
    "build",
    "coverage",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    ".vs",
    ".tox",
    ".nox",
    ".next",
    ".nuxt",
    ".cache",
    "target",
    "vendor",
    "generated",
    ".ssh",
    ".aws",
    ".kube",
    "secrets",
    "credentials",
}
EXCLUDED_NAMES = {"package-lock.json", "pnpm-lock.yaml", "composer.lock", "cargo.lock"}
SENSITIVE_NAME = re.compile(r"(^|[._-])(secrets?|credentials?|service[._-]?account)([._-]|$)")
SECRET_CONTENT = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{20,}|"
    r"\bgithub_pat_[A-Za-z0-9_]{20,}"
)


@dataclass(frozen=True)
class SourceFile:
    path: str
    language: str
    content: str


class RepositoryScanner:
    def __init__(self, max_file_bytes: int = 262144, max_line_chars: int = 8000) -> None:
        if max_file_bytes < 1 or max_line_chars < 1:
            raise ValueError("invalid_scan_limits")
        self.max_file_bytes = max_file_bytes
        self.max_line_chars = max_line_chars

    def scan(self, root: Path) -> Iterator[SourceFile]:
        if root.is_symlink() or root.is_junction() or not root.is_dir():
            raise ValueError("invalid_scan_root")
        root = root.resolve()

        def walk_error(error: OSError) -> None:
            raise error  # A partial scan must never delete previously indexed chunks.

        for directory, directories, files in os.walk(root, followlinks=False, onerror=walk_error):
            parent = Path(directory)
            directories[:] = sorted(
                name
                for name in directories
                if name.lower() not in EXCLUDED_DIRECTORIES
                and not name.lower().endswith((".egg-info", ".dist-info"))
                and not (parent / name).is_symlink()
                and not (parent / name).is_junction()
            )
            for name in sorted(files):
                path = parent / name
                lower = name.lower()
                language = LANGUAGES.get(path.suffix.lower())
                if (
                    language is None
                    or lower.startswith(".env")
                    or lower in EXCLUDED_NAMES
                    or SENSITIVE_NAME.search(lower)
                    or any(
                        marker in lower
                        for marker in (".min.", ".generated.", "_pb2.", "_pb2_grpc.")
                    )
                    or path.is_symlink()
                    or path.is_junction()
                ):
                    continue
                if not path.resolve().is_relative_to(root):
                    raise ValueError("unsafe_scan_path")
                metadata = path.stat(follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > self.max_file_bytes:
                    continue
                with path.open("rb") as stream:
                    raw = stream.read(self.max_file_bytes + 1)
                if len(raw) > self.max_file_bytes:
                    continue
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue  # Non-UTF-8 and binary files are intentionally unsupported.
                if any(ord(char) < 32 and char not in "\t\r\n" for char in content):
                    continue
                header = content[:2048].lower()
                if (
                    "@generated" in header
                    or "do not edit" in header
                    or SECRET_CONTENT.search(content)
                    or any(
                        len(line) > self.max_line_chars
                        for line in content.splitlines(keepends=True)
                    )
                ):
                    continue
                if content.strip():
                    yield SourceFile(path.relative_to(root).as_posix(), language, content)
