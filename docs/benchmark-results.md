# Retrieval scale benchmark — October 9, 2026

**PASS for the measured synthetic workloads.** This is not a production load or real-model
quality certification. Production code was unchanged. Reproduce with the commands in
[testing.md](testing.md#opt-in-scale-benchmark).

Environment: Windows, Python 3.12.10, Intel i5-12600K (10 cores / 16 logical processors),
approximately 31.7 GiB host RAM. Docker Linux engine: 16 CPUs, approximately 15.5 GiB RAM.
PostgreSQL uses `pgvector/pgvector:pg16`. Each run creates, migrates and drops an isolated
database and uses a temporary committed Git repository. Database transactions really commit;
the benchmark does not wrap writes in a rollback-only test transaction.

Before measurement, budgets were set to initial indexing <=300 seconds, each retriever's
p95 <=3 seconds, and peak Python process memory <=1024 MiB. Twenty sequential queries are
measured per retriever, top_k=10. Each synthetic Python file has 100 lines, 20 functions and
a distinct module identifier. Default chunking produces one chunk per file. Embedding
generation uses the deterministic fake provider with 1536 dimensions and batches of 16.

| Measurement | 1,000 files | 5,000 files |
| --- | ---: | ---: |
| Lines | 100,000 | 500,000 |
| Source bytes | 2,758,325 | 13,878,325 |
| Initial chunks | 1,000 | 5,000 |
| Initial indexing, seconds | 8.635 | 44.841 |
| Unchanged indexing, seconds | 1.640 | 5.906 |
| Fake embedding generation, seconds | 2.695 | 13.771 |
| Incremental indexing, seconds | 1.622 | 6.284 |
| BM25 p50 / p95, seconds | 0.211 / 0.239 | 1.093 / 1.219 |
| Vector p50 / p95, seconds | 0.008 / 0.013 | 0.021 / 0.026 |
| Hybrid p50 / p95, seconds | 0.228 / 0.303 | 1.069 / 1.160 |
| Peak Python process MiB | 102.21 | 158.30 |
| Target file found in top 10, each retriever | 20/20 | 20/20 |
| Files changed / deleted | 10 / 10 | 50 / 50 |
| Final chunks | 990 | 4,950 |
| Budget result | PASS | PASS |

Both runs verify that unchanged chunk IDs survive re-indexing, changed chunk IDs are
replaced, deleted files disappear, unchanged embeddings are not regenerated, and only
the changed chunks are re-embedded. Returned IDs belong to the measured repository.
Existing separate repository-isolation tests remain relevant; this benchmark itself uses
one repository per database. Run durations: 30.61 seconds and 138.12 seconds respectively.
The 1,000-file measurement was repeated after adding memory measurement; the table uses
that final run, not the faster preliminary runs.

Raw final measurements and pytest XML are in `.pytest_cache/benchmark-1000.json`,
`.pytest_cache/benchmark-5000.json`, and matching XML files. They are generated/ignored.
The executable fixture is `tests/integration/test_scale_benchmark.py`.

Limits: single-process synthetic Python, local warm database, 20 samples rather than a
statistically extensive load study. Peak memory includes pytest, excludes PostgreSQL/Docker,
and does not establish bounded memory as repository size grows. Fake vectors test plumbing
and lexical feature similarity, not real semantic ranking or provider latency/cost. No
multi-worker throughput or diverse real monorepo was measured. BM25 still rebuilds its
corpus per query; indexing still materializes all chunks; vectors still use exact search.

A defensible scale statement is: "Benchmarked indexing and hybrid retrieval on a synthetic
5,000-file / 500,000-line repository, with 1.16-second hybrid p95 on the recorded machine."
Do not generalize this to arbitrary large repositories or production concurrency.

## Remaining delivery gates

1. **Disposable real integration: BLOCKED.** Local Settings currently has no database,
   Redis/Celery URLs, GitHub token, LLM key, LLM model or embedding model. Docker services
   are healthy; no Docker installation action is needed. Configure `.env` locally using
   `.env.example` and [provider setup](development.md#github-and-llm-setup), and identify an
   authorized disposable GitHub repository. No real model call, push or PR was performed.
2. **Hosted CI: PENDING**, after the real integration as requested. Local Ruff, formatting
   and mypy passed; these are not evidence of a GitHub Actions run on the eventual release
   commit.
3. **Release: PENDING**, after both earlier gates. No release tag or GitHub release was
   published. No production deployment is implied.

Suggested commit message: `test: benchmark indexing and retrieval at 5000 files`.

## Changes and verification

Created: this report and `tests/integration/test_scale_benchmark.py`.
Modified: `.env.example` (documented optional process flags), `docs/testing.md`
(reproduction commands), and `docs/final-acceptance.md` (follow-up evidence notice).
Pre-existing README/development documentation edits were preserved. No production code,
schema, external service adapter, workflow configuration or dependency was changed.

Executed `docker compose up -d --wait --wait-timeout 180`, the two final benchmark pytest
commands documented above, and:

```powershell
python -m pytest tests/unit -q --junitxml=.pytest_cache/benchmark-unit.xml
python -m ruff check .
python -m ruff format --check .
python -m mypy
git diff --check
```

Final selected runs: **327 passed, 0 failed, 0 skipped** (325 unit tests plus two benchmark
invocations). Unit tests took 29.62 seconds, with two existing Starlette/AnyIO deprecation
warnings. Ruff and formatting passed; mypy passed on 199 source files. The complete
integration/end-to-end suite was not repeated in this benchmark-only change; its earlier
results remain in the final acceptance report. PostgreSQL confirmed zero remaining
`platform_test_*` databases after benchmark cleanup. Normal CI will intentionally skip the
new benchmark unless its opt-in flag is set.
