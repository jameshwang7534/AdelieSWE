"""Opt-in synthetic scale measurement using the real persistence/retrieval pipeline."""

import ctypes
import importlib
import json
import math
import os
import platform
import statistics
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import create_session_factory, session_scope
from app.indexing.embeddings import EmbeddingService
from app.indexing.scanner import RepositoryScanner
from app.indexing.service import IndexingService
from app.integrations.git.runner import SubprocessGitRunner
from app.integrations.llm.fake_embeddings import FakeEmbeddingProvider
from app.models import CodeChunk, Repository
from app.retrieval.bm25 import BM25Retriever
from app.retrieval.hybrid import HybridRetrievalService
from app.retrieval.postgres import PostgresChunkSource
from app.retrieval.vector import VectorRetriever
from app.services.workspace import WorkspaceService
from app.services.workspace_sync import prepare_registered_repository
from tests.integration.test_database import engine as engine

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_SCALE_BENCHMARK") != "1",
    reason="Opt-in scale benchmark: RUN_SCALE_BENCHMARK=1 and TEST_DATABASE_URL required",
)


def source(index: int) -> str:
    """100 deterministic lines per file, with a distinct searchable identifier."""
    return "".join(
        f"def module_{index:05d}_operation_{operation}(value):\n"
        f'    """Transform module_{index:05d} input for operation {operation}."""\n'
        f"    result = value + {index + operation}\n"
        "    return result\n\n"
        for operation in range(20)
    )


def snapshot(sessions: sessionmaker[Session], identifier: UUID) -> dict[str, UUID]:
    with session_scope(sessions) as session:
        rows = session.execute(
            select(CodeChunk.file_path, CodeChunk.id).where(CodeChunk.repository_id == identifier)
        )
        return {row.file_path: row.id for row in rows}


def peak_process_mib() -> float:
    """OS high-water mark, including pytest overhead but excluding database/Docker RAM."""
    if platform.system() != "Windows":
        resource = importlib.import_module("resource")
        peak = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return peak / (1024 * 1024 if platform.system() == "Darwin" else 1024)

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("faults", ctypes.c_ulong),
            ("peak", ctypes.c_size_t),
            ("working", ctypes.c_size_t),
            ("peak_paged", ctypes.c_size_t),
            ("paged", ctypes.c_size_t),
            ("peak_nonpaged", ctypes.c_size_t),
            ("nonpaged", ctypes.c_size_t),
            ("pagefile", ctypes.c_size_t),
            ("peak_pagefile", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(Counters),
        ctypes.c_ulong,
    ]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(
        kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return float(counters.peak) / (1024 * 1024)


def test_scale_pipeline(engine: Engine, local_repository: Path, tmp_path: Path) -> None:
    factory = create_session_factory(engine)
    count = int(os.environ.get("BENCHMARK_FILES", "1000"))
    assert 100 <= count <= 10000
    byte_count = 0
    for index in range(count):
        content = source(index)
        byte_count += len(content.encode())
        (local_repository / f"module_{index:05d}.py").write_text(
            content, encoding="utf-8", newline="\n"
        )
    git = SubprocessGitRunner()
    git.run(["add", "."], cwd=local_repository)
    git.run(
        [
            "-c",
            "user.name=Benchmark",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "Synthetic corpus",
        ],
        cwd=local_repository,
    )
    with session_scope(factory) as session:
        repository = Repository(
            github_owner="benchmark",
            github_name=str(uuid4()),
            clone_url=str(local_repository),
            default_branch="main",
        )
        session.add(repository)
        session.flush()
        identifier = repository.id
    workspace = WorkspaceService(tmp_path / "workspaces", git, allow_local=True)
    prepare_registered_repository(identifier, factory, workspace)
    indexer = IndexingService(factory, workspace, RepositoryScanner())
    started = perf_counter()
    initial = indexer.index(identifier)
    initial_seconds = perf_counter() - started
    before = snapshot(factory, identifier)
    assert len(before) == count == initial["chunks"]
    started = perf_counter()
    indexer.index(identifier)
    unchanged_seconds = perf_counter() - started
    assert snapshot(factory, identifier) == before
    provider = FakeEmbeddingProvider()
    embeddings = EmbeddingService(factory, workspace, provider, batch_size=16)
    started = perf_counter()
    assert embeddings.generate(identifier)["embedded"] == count
    embedding_seconds = perf_counter() - started
    assert embeddings.generate(identifier)["embedded"] == 0

    lexical = BM25Retriever(PostgresChunkSource(factory))
    vector = VectorRetriever(factory, provider)
    hybrid = HybridRetrievalService(lexical, vector)
    timings: dict[str, dict[str, float]] = {}
    for name, retriever in (("bm25", lexical), ("vector", vector), ("hybrid", hybrid)):
        samples = []
        hits = 0
        for position in range(20):
            target = position * (count - 1) // 19
            started = perf_counter()
            results = retriever.search(identifier, f"module_{target:05d}", top_k=10)
            samples.append(perf_counter() - started)
            assert len(results) == 10
            assert all(result.chunk_id in before.values() for result in results)
            hits += any(result.file_path == f"module_{target:05d}.py" for result in results)
        timings[name] = {
            "p50_seconds": statistics.median(samples),
            "p95_seconds": sorted(samples)[math.ceil(len(samples) * 0.95) - 1],
            "target_hits_at_10": hits,
        }
        if name == "bm25":
            assert hits == 20

    root = workspace.repository_path(identifier)
    affected = count // 100
    for index in range(affected):
        (root / f"module_{index:05d}.py").write_text(
            source(index).replace("value +", "value -"), encoding="utf-8", newline="\n"
        )
    for index in range(affected, affected * 2):
        (root / f"module_{index:05d}.py").unlink()
    started = perf_counter()
    indexer.index(identifier)
    incremental_seconds = perf_counter() - started
    after = snapshot(factory, identifier)
    assert len(after) == count - affected
    assert all(after[f"module_{i:05d}.py"] != before[f"module_{i:05d}.py"] for i in range(affected))
    assert all(
        after[f"module_{i:05d}.py"] == before[f"module_{i:05d}.py"]
        for i in range(affected * 2, count)
    )
    assert embeddings.generate(identifier)["embedded"] == affected
    # Budgets declared before measuring; failures preserve the measurement report.
    memory = peak_process_mib()
    budgets = {"initial_index_seconds": 300, "query_p95_seconds": 3, "peak_process_mib": 1024}
    passed = (
        memory <= budgets["peak_process_mib"]
        and initial_seconds <= budgets["initial_index_seconds"]
        and all(
            timing["p95_seconds"] <= budgets["query_p95_seconds"] for timing in timings.values()
        )
    )
    report = {
        "python": platform.python_version(),
        "platform": platform.system(),
        "logical_cpus": os.cpu_count(),
        "peak_process_mib": memory,
        "files": count,
        "lines": count * 100,
        "source_bytes": byte_count,
        "chunks": initial["chunks"],
        "initial_index_seconds": initial_seconds,
        "unchanged_index_seconds": unchanged_seconds,
        "embedding_seconds_fake_provider": embedding_seconds,
        "incremental_index_seconds": incremental_seconds,
        "changed_files": affected,
        "deleted_files": affected,
        "final_chunks": len(after),
        "queries_per_retriever": 20,
        "retrieval": timings,
        "budgets": budgets,
        "budget_pass": passed,
        "limitations": [
            "Synthetic Python only",
            "Fake embeddings, no semantic quality evidence",
            "Single process, no concurrency",
            "Database/Docker memory excluded",
            "No network/provider latency",
            "Warm local database",
        ],
    }
    output = Path(".pytest_cache") / f"benchmark-{count}.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    assert passed, f"Scale budget exceeded; measurements saved to {output}"
