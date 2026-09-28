"""Strict subset of Git unified diffs: regular UTF-8 text additions/edits/deletions only."""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath


class PatchError(Exception):
    def __init__(self, code: str, diagnostic: str = "") -> None:
        super().__init__(code)
        self.code, self.diagnostic = code, diagnostic[:4000]


def validate_path(value: str) -> str:
    parts = value.split("/")
    if (
        len(value) > 512
        or PurePosixPath(value).is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", value)
    ):
        raise PatchError("unsafe_patch_path")
    for part in parts:
        name = part.lower()
        stem = name.split(".")[0]
        if (
            name.endswith(".")
            or name.startswith(".env")
            or name
            in {
                ".git",
                ".gitmodules",
                ".gitattributes",
                ".gitconfig",
                ".ssh",
                ".aws",
                ".kube",
                ".docker",
                ".netrc",
                ".npmrc",
                ".pypirc",
                "credentials",
                "credentials.json",
                "secrets.json",
                "secrets.yaml",
                "secrets.yml",
            }
            or name.endswith((".pem", ".key", ".p12", ".pfx"))
            or stem
            in {
                "con",
                "prn",
                "aux",
                "nul",
                *(f"com{i}" for i in range(1, 10)),
                *(f"lpt{i}" for i in range(1, 10)),
            }
        ):
            raise PatchError("protected_patch_path")
    return value


@dataclass(frozen=True)
class PatchFile:
    path: str
    new: bool
    deleted: bool


def parse_patch(patch: str, files: tuple[str, ...]) -> list[PatchFile]:
    if "\x00" in patch or len(patch.encode("utf-8")) > 262144:
        raise PatchError("invalid_patch_format")
    declared = [validate_path(path) for path in files]
    if len(set(path.casefold() for path in declared)) != len(declared):
        raise PatchError("duplicate_patch_paths")
    lines = patch.splitlines(keepends=True)
    output: list[PatchFile] = []
    i = 0
    while i < len(lines):
        header = re.fullmatch(r"diff --git a/(\S+) b/(\S+)\n", lines[i])
        if header is None or header[1] != header[2]:
            raise PatchError(
                "unsupported_patch_header", f"Invalid file header at patch line {i + 1}"
            )
        path = validate_path(header[1])
        i += 1
        new = deleted = False
        if i < len(lines) and lines[i] in {"new file mode 100644\n", "deleted file mode 100644\n"}:
            new, deleted = lines[i].startswith("new"), lines[i].startswith("deleted")
            i += 1
        if i < len(lines) and re.fullmatch(
            r"index [0-9a-f]+\.\.[0-9a-f]+(?: 100(?:644|755))?\n", lines[i]
        ):
            i += 1
        old_header = "/dev/null" if new else f"a/{path}"
        new_header = "/dev/null" if deleted else f"b/{path}"
        if lines[i : i + 2] != [f"--- {old_header}\n", f"+++ {new_header}\n"]:
            raise PatchError("unsupported_patch_header")
        i += 2
        hunks = 0
        while i < len(lines) and lines[i].startswith("@@ "):
            match = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n", lines[i])
            if match is None:
                raise PatchError("invalid_patch_hunk")
            old_count, new_count = int(match[2] or 1), int(match[4] or 1)
            i += 1
            old = added = 0
            while i < len(lines) and (old < old_count or added < new_count):
                line = lines[i]
                if not line or line[0] not in " +-":
                    raise PatchError("invalid_patch_hunk")
                old += line[0] in " -"
                added += line[0] in " +"
                i += 1
                if i < len(lines) and lines[i] == "\\ No newline at end of file\n":
                    i += 1
            if old != old_count or added != new_count:
                raise PatchError("invalid_patch_hunk")
            hunks += 1
        if not hunks:
            raise PatchError("empty_patch_file")
        output.append(PatchFile(path, new, deleted))
    if len(output) != len(declared) or {item.path for item in output} != set(declared):
        raise PatchError("patch_file_list_mismatch")
    return output
