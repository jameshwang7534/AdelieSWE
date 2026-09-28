"""Actual local Git patch validation and application, without any external service."""

import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from app.integrations.git.runner import SubprocessGitRunner
from app.sandbox.base import SandboxError
from app.schemas.coding import CodeChangeProposal
from app.services.code_patches import CodePatchService
from app.services.patch_format import PatchError, parse_patch, validate_path

EDIT = """diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-original
+changed
"""
NEW = """diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1 @@
+answer = 42
"""
DELETE = """diff --git a/hello.txt b/hello.txt
deleted file mode 100644
--- a/hello.txt
+++ /dev/null
@@ -1 +0,0 @@
-original
"""


def change(diff: str = EDIT, files: tuple[str, ...] = ("hello.txt",)) -> CodeChangeProposal:
    return CodeChangeProposal(
        summary="Focused fixture change",
        files_changed=files,
        unified_diff=diff,
        assumptions=(),
        tests_to_run=("Check output",),
    )


def workspace(root: Path, source: Path) -> Path:
    target = root / "executions" / str(uuid4()) / str(uuid4())
    shutil.copytree(source, target)
    return target


@pytest.mark.parametrize(
    "diff,files",
    [
        (EDIT, ("hello.txt",)),
        (NEW, ("new.py",)),
        (DELETE, ("hello.txt",)),
        (EDIT + NEW, ("hello.txt", "new.py")),
    ],
)
def test_apply_collect_diff(
    tmp_path: Path, local_repository: Path, diff: str, files: tuple[str, ...]
) -> None:
    path = workspace(tmp_path, local_repository)
    service = CodePatchService(tmp_path)
    with service.locked(path):
        assert service.status(path) == "clean"
        observed = service.apply(path, change(diff, files))
        assert observed and all(name in observed for name in files)
        if diff == DELETE:
            assert not (path / "hello.txt").exists()
        elif "hello.txt" in files:
            assert (path / "hello.txt").read_text() == "changed\n"
        if "new.py" in files:
            assert (path / "new.py").read_text() == "answer = 42\n"
    assert not path.with_name(f".{path.name}.coding.lock").exists()


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/x",
        "../x",
        "a/../../x",
        "C:/x",
        "a\\x",
        "a//x",
        ".git/config",
        ".GIT/hooks/test",
        ".env",
        ".env.example",
        "src/.aws/credentials",
        "secret.pem",
        ".gitattributes",
        "nul.py",
        "src/x.",
        "x:stream",
        "./x",
    ],
)
def test_path_rejection(path: str) -> None:
    with pytest.raises(PatchError):
        validate_path(path)


def test_conflicting_patch_changes_nothing(tmp_path: Path, local_repository: Path) -> None:
    path = workspace(tmp_path, local_repository)
    service = CodePatchService(tmp_path)
    with service.locked(path), pytest.raises(PatchError) as failure:
        service.apply(
            path, change(NEW + EDIT.replace("-original", "-nonexistent"), ("new.py", "hello.txt"))
        )
    assert failure.value.code == "patch_check_failed" and failure.value.diagnostic
    assert (path / "hello.txt").read_text() == "original\n" and not (path / "new.py").exists()


@pytest.mark.parametrize(
    "diff,files",
    [
        (EDIT, ("other.py",)),
        (EDIT + EDIT, ("hello.txt",)),
        (EDIT.replace("@@ -1 +1 @@", "@@ -2,2 +1 @@"), ("hello.txt",)),
        (NEW.replace("100644", "120000"), ("new.py",)),
        (EDIT.replace("b/hello.txt", "b/elsewhere"), ("hello.txt",)),
    ],
)
def test_unsupported_or_malformed_diff(diff: str, files: tuple[str, ...]) -> None:
    with pytest.raises(PatchError):
        parse_patch(diff, files)


def test_busy_workspace_and_dangerous_config(tmp_path: Path, local_repository: Path) -> None:
    path = workspace(tmp_path, local_repository)
    service = CodePatchService(tmp_path)
    with (
        service.locked(path),
        pytest.raises(PatchError, match="coding_workspace_busy"),
        service.locked(path),
    ):
        pytest.fail("Second lock must not succeed")
    SubprocessGitRunner().run(["config", "filter.malicious.clean", "never-execute"], cwd=path)
    with pytest.raises(PatchError, match="unsafe_local_git_configuration"), service.locked(path):
        pytest.fail("Must reject custom Git execution configuration")


def test_hardlink_target_rejected(tmp_path: Path, local_repository: Path) -> None:
    path = workspace(tmp_path, local_repository)
    external = tmp_path / "outside"
    external.write_text("original\n")
    (path / "hello.txt").unlink()
    (path / "hello.txt").hardlink_to(external)
    with pytest.raises(SandboxError), CodePatchService(tmp_path).locked(path):
        pytest.fail("Hardlinks must not be patched")
    assert external.read_text() == "original\n"


def test_edit_previously_created_untracked_file(tmp_path: Path, local_repository: Path) -> None:
    path = workspace(tmp_path, local_repository)
    service = CodePatchService(tmp_path)
    with service.locked(path):
        service.apply(path, change(NEW, ("new.py",)))
        edit = EDIT.replace("hello.txt", "new.py").replace("-original", "-answer = 42")
        observed = service.apply(path, change(edit, ("new.py",)))
    assert "new.py" in observed and "+changed" in observed
    assert (path / "new.py").read_text() == "changed\n"
