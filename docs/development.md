# Development from a new checkout

## Prerequisites and installation

Obtain this repository through your normal Git checkout process, then open its root.
You need Python 3.12.x, Git and Docker with Linux containers. Docker Compose v2 is required.
The host needs access to package/image registries for installation. Tests use local services
and fake external providers after dependencies and the trusted image are installed.

**MANUAL ACTION REQUIRED — only when a prerequisite is missing:** install Python 3.12
and Git on your development computer; install/start Docker Desktop and select Linux
containers on Windows. Verify in a new terminal with `python --version`, `git --version`,
`docker version` and `docker info --format '{{.OSType}}'`. Expected output is Python 3.12.x,
a Git version, Docker Client **and Server** information, and `linux`. On Linux, use a
working Docker engine; access to its socket is privileged and belongs only on trusted hosts.

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

If `python` is another version, use `py -3.12 -m venv .venv` on Windows. If activation is
blocked, use `.\.venv\Scripts\python.exe` instead of `python` in every command; no machine-wide
execution-policy change is needed. In each new terminal, activate the same environment.

Linux/macOS shell equivalent:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
test -f .env || cp .env.example .env
```

Keep `.env` local. The configuration factory reads it relative to the current working
directory, so start API/workers/Beat/migrations from the repository root. Copying the example
configures local services; it does not configure real model credentials.

## Infrastructure and schema

```sh
docker compose config --quiet
docker compose up -d --wait --wait-timeout 180
docker compose ps
python -m alembic upgrade head
python -m alembic current
docker compose exec -T redis redis-cli ping
docker pull python:3.12-slim
```

Verify the extension with the cross-platform Python command below, which reads the
configured URL without printing it:

```powershell
python -c "from sqlalchemy import text; from app.core.config import Settings; from app.db.session import create_database_engine; e=create_database_engine(Settings()); c=e.connect(); print(c.scalar(text('SELECT extversion FROM pg_extension WHERE extname=:name'), {'name':'vector'})); c.close(); e.dispose()"
```

Expect a pgvector version and Redis `PONG`. The Postgres initialization script runs only
for a fresh volume. Alembic remains necessary on both fresh and existing databases.
Do not assume changing POSTGRES_PASSWORD reconfigures an already-initialized volume.
Update host URLs whenever credentials/ports change; URL-encode reserved characters.

Use `python -m alembic heads` to inspect the migration target. Downgrade removes data;
only use `python -m alembic downgrade base` on a disposable database. Migration tests cover
downgrade/re-upgrade without erasing the development database. pgvector is retained on
downgrade because other schemas may use it. Schema dimension changes require a migration.

## API, workers and Beat

In separate activated root terminals:

```sh
python -m uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000
python -m celery -A app.workers.celery_app:app worker --pool=solo --concurrency=1 -Q orchestration,indexing,agents --loglevel=INFO
python -m celery -A app.workers.celery_app:app beat --loglevel=INFO
```

These are separate long-running commands, not a sequential script. Run only one Beat
scheduler. `solo` is the documented Windows development mode; Celery process time limits
are not enforced by that pool. Use Linux/prefork workers for process isolation/time limits,
for example omit `--pool=solo` and choose `--concurrency=2`. Docker enforces its own
execution timeout regardless. API and workers must share the same managed workspace
storage and configuration. Multiple machines require deliberately configured shared paths;
the repository does not provision distributed storage or production containers.

Verify `/health`, `/ready`, then POST `/tasks/ping` and GET `/tasks/{task_id}` in Swagger UI.
Expect successful dependency checks and eventual `SUCCESS` with diagnostic metadata.
An unknown Celery task ID can appear PENDING; it does not prove a task was submitted.
Persisted workflow/delivery APIs are preferable for durable business progress.

## GitHub and LLM setup

**MANUAL ACTION REQUIRED — only for real repository/provider use:**

1. In GitHub account settings, create a repository-scoped fine-grained personal access
   token for repositories you are authorized to operate on. Imports need Metadata/Issues
   read; private clones need Contents read; publication needs Contents and Pull requests
   write. Obtain organization approval/SSO authorization if your organization requires it.
2. Store it in local `.env` as `GITHUB_TOKEN`, never in a remote URL or chat. Set
   `GITHUB_API_URL` and `GITHUB_GIT_HOST` together for an approved Enterprise deployment.
   Restart API/workers. Verify by registering the repository with the README's
   `POST /repositories` example, importing one issue, and running
   `repository.prepare_workspace`. Expect a repository UUID, issue JSON, then task SUCCESS
   with a ready workspace and commit SHA. This does not create a PR.
3. In your chosen provider's account, obtain access to an HTTPS OpenAI-compatible endpoint
   supporting chat completions with JSON-schema structured output and embeddings. Put its
   credential in `LLM_API_KEY`; configure `LLM_BASE_URL`, `LLM_MODEL` and `EMBEDDING_MODEL`
   locally. Both adapters share that key/base URL. No provider/model is selected by an
   unused `*_PROVIDER` placeholder. Default base URL, when omitted, is the OpenAI v1 API.
4. Ensure embeddings return 1536 dimensions; configure `EMBEDDING_SEND_DIMENSIONS=false`
   only if the endpoint omits support for that parameter and the model still returns the
   required size. Restart processes. Verify with the README's embedding task followed by
   POST `/repositories/{id}/search/vector` and POST `/issues/{id}/plan`. Expect task SUCCESS,
   ranked chunks (when matching indexed content exists), and a saved plan ID. These calls
   send repository content to your provider and can incur charges.

No keys are needed for the normal unit suite. Missing model configuration is surfaced when
the relevant provider is used. Health/readiness are not credential verification.

## Test images and policy

The supplied `python:3.12-slim` image contains unittest, not pytest. Workflow defaults run
`python -m unittest` through the trusted command policy. Review required test commands for
each deployment: a green run with zero discovered tests is not meaningful validation.
Prebuild/pre-pull reviewed images containing the repository's dependencies, then configure
`SANDBOX_IMAGES` aliases and permitted commands. Do not enable arbitrary shell commands
or runtime network package installation to work around missing dependencies.

## Checks and troubleshooting

Use [testing.md](testing.md) for exact fast, integration, end-to-end and all-test commands.
No separate test worker is needed; worker tests manage their own workers/unique queues.

* Readiness 503: check configured host/port/credentials and Compose health.
* Database relation missing: apply Alembic migrations; readiness alone does not check schema.
* Workflow stays pending: verify all three queues are consumed and Beat is running.
* Index/context 409: wait for successful sync/source indexing, then embedding generation.
* Provider 503: inspect sanitized error codes and local configuration; do not print keys.
* Sandbox failure: verify the image is already present and Docker is reachable by the
  worker. Non-root UID 1000 must have required access to the specific workspace on Linux.
* Stale lock or ambiguous publication: stop conflicting work and inspect persisted status
  before recovery. Do not delete active locks or blindly replay external side effects.

`docker compose stop` stops services without deleting volumes. Interrupt API/worker/Beat
terminals separately. Logs are JSON with available IDs; storage/rotation and workspace
retention are operator responsibilities.
