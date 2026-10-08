# Security controls and operating limits

## Trust boundaries

The API currently has no built-in authentication, tenant authorization or rate limiting.
Bind it to loopback for development. An externally reachable deployment needs an access
control boundary, TLS, dedicated credentials and an operator-reviewed exposure model.
Compose defaults are development-only; PostgreSQL/Redis bind to loopback and use persistent
volumes. Redis is not configured with authentication/TLS by this Compose file.

The worker can access GitHub/model credentials, source checkouts, the database and Docker.
Docker control is a powerful host privilege. Run on dedicated trusted machines, never
expose the Docker socket to repository containers, and do not treat this implementation
as a hardened hostile multi-tenant service.

## Credentials and data

`GITHUB_TOKEN` and `LLM_API_KEY` belong in an untracked `.env` or managed process environment.
Settings wrap secrets. Git authentication uses a temporary askpass helper rather than
embedding credentials in URLs. Git hooks, interactive credential prompts and redirects are
restricted. Code executing in Docker receives a restricted environment, not the worker's
environment wholesale. Do not paste tokens into chat or CLI arguments.

JSON logs allowlist metadata, redact configured secrets/URLs/authorization assignments,
and omit exception text, prompts, completions and test-output bodies. Third-party free-text
messages are suppressed. This applies to configured logging handlers, not arbitrary print
statements or future unmanaged handlers. See [observability](observability.md).

Source chunks, issue bodies, plans, patches and test evidence can be sensitive. They are
persisted in PostgreSQL/workspaces and may be sent to the configured LLM/embedding provider.
Scanner exclusions are heuristic, not a complete secret detector. Ordinary source/config
files can still contain credentials. Review repository/provider data permissions and set
retention/backup/access policies before real use; automatic retention cleanup is absent.

## Untrusted patches and test commands

Patch validation rejects absolute/traversing paths, Git internals, protected secret files
and unsafe repository paths. Patches must apply cleanly; failures preserve diagnostic state
and do not count as task success. Repository text and issue content remain potential prompt
injection inputs: structured schemas and bounded context are not proof of safe intent.

TestAgent accepts exact operator-enabled command profiles rather than arbitrary LLM shell
strings. The policy passes an argument vector to Docker, not an unrestricted host shell.
Allowed commands such as `npm test` still execute repository-controlled scripts. Thus the
sandbox is necessary even when a command name is approved. Meaningful tests and image
contents must be chosen by the operator; generated code can still weaken its own tests.

## Docker controls

DockerSandbox uses pre-pulled trusted image aliases resolved to image IDs. It sets a
non-root user, disables network, drops Linux capabilities, enables no-new-privileges, uses
a read-only container root, CPU/memory/PID limits and a bounded execution timeout. Only the
validated task workspace is bind-mounted; temporary memory-backed storage is provided.
The workspace is writable because tests may create artifacts. Image environment and proxy
variables are not used as an excuse to forward host credentials.

Outputs are bounded, exit status/stdout/stderr/timeouts are captured, and cleanup is
attempted in the execution lifecycle. A worker/host crash can still require operator cleanup.
The container does not mount the host root or Docker socket and does not use privileged
mode. Kernel/container escapes, denial of service beyond configured limits and malicious
test behavior are residual risks, not solved by a passing sandbox test.

## Publication and recovery

Publication defaults off and must be enabled in the original workflow request. The service
requires completed required tasks, passing required tests, current nonblocking review and
actual workspace changes. It uses a dedicated deterministic branch, avoids direct default
branch commits/force-pushes, records publication attempts and reconciles remote state after
ambiguous failures. It does not auto-merge. Human PR review remains necessary.

Retry budgets, persisted claims and state guards reduce duplicate effects; they do not form
an atomic transaction across GitHub, Docker, Git and PostgreSQL. Cancellation occurs at safe
boundaries and does not retract remote writes. Inspect blocked/stale/ambiguous records before
manual recovery. See [orchestration reliability](orchestration-reliability.md).

CI uses ephemeral hosted Linux runners, read-only checkout permissions and fake providers.
Do not run untrusted pull requests on sensitive self-hosted runners or give fixture jobs
production secrets. The Python network guard in tests is not an OS-level firewall.
