# Container and desktop deployment

Both packages run the normal built wheel, owner and MCP bridge. Build provenance
is visible in `server_status` and `teleloom doctor`. Neither package includes an
account, credentials, local evidence or permission grants.

For a fresh native owner, follow [installation through one bounded read](install-ru.md).
For an existing owner, retain its installed interpreter, data directory and client
credential environment; use the [upgrade contract](release-pipeline.md#selecting-and-preserving-an-owner)
only when upgrading, then the [same first-read check](install-ru.md#5-проверьте-identity-и-одно-чтение).
Deployment packaging is already available; changing format does not provision an
identity or grant read/send/mutation/AI/event access.

Use one chosen wheel and its matching lock. For a release, reuse the verified
artifact from [release preparation](release-pipeline.md); do not rebuild it for
each deployment format. For packaging development, build once in a separate
tooling environment with `uv build`, then reuse `dist/` for the steps below.

## Desktop bundle

```text
uv run python scripts/package_desktop.py --wheel dist/teleloom-0.5.0-py3-none-any.whl --output dist/teleloom.mcpb
npx --yes @anthropic-ai/mcpb@2.1.2 validate packaging/desktop/manifest.json
```

The MCPB archive contains the exact wheel, a UV dependency lock, a small launcher
and the manifest. Install it in a host that supports
[UV MCPB servers](https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md).
Select the private data directory already provisioned by `teleloom init` and local
authentication. Its OS credential store and owner permissions remain authoritative.
The manifest has no credential input; account provisioning stays in the local CLI.
Its UV environment is fixed to the extracted bundle's `.venv`, so an inherited
`UV_PROJECT_ENVIRONMENT` cannot replace another project's installed package.
Use the same OS user as the existing owner. Reconnect the client after an intentional
package upgrade and compare build IDs. In that host, inspect `profiles_list`
(identity/backend, read policy, separate grants and exposure), then resolve one
readable chat and call `messages_get` with `limit=10`. Preserve original identities
and report source/coverage. Bot history remains saved observations.

The automated test extracts the archive into a path containing spaces and opens
real stdio MCP outside the checkout, with disposable state and no accounts. It
establishes launcher and artifact behavior. Installation in a particular desktop
host still requires that host's acceptance check.

## Docker and Compose

```text
docker build --tag teleloom-local .
```

The image installs the wheel and runs as UID 10001. The Compose configuration
uses a read-only root filesystem, drops capabilities, exposes no host port and
keeps owner state in a private named volume. A shared `teleloom-session-locks`
volume coordinates containers using the same Telegram authorization. Keep that
volume shared across Compose projects using the same accounts. The container
cannot coordinate with a separate host-native owner: stop that owner before
using the same authorization in a container.

Have your platform's secret manager provide a private file and set
`TELELOOM_MCP_TOKEN_FILE` to its absolute path in the calling process environment.
Compose bind-mounts this file read-only; environment-backed Compose secrets cannot
be injected into a read-only service. Keep the file outside the checkout and image
build context. On Linux, its private parent directory must restrict host access,
and the file must be readable by container UID 10001; Compose does not remap
file-secret ownership or permissions. On Windows, restrict the host file's ACL to
the operator. Remove an ephemeral file only after stopping the service. The entry
point reads only bounded `TELELOOM_*` mounts and forwards them into the existing
environment credential seam; the application does not gain a plaintext credential
store. Values must not appear in commands, YAML, the image or data volume.
See [Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/).

```text
docker compose run --rm -T teleloom init
docker compose up --detach
docker compose exec -T teleloom python /opt/teleloom-container.py doctor --mode mcp
```

This creates an empty owner. Provision the private `/data/config.json` with the
intended profile definitions and permission lists before starting an account.
An existing configuration contains metadata and permissions; session credentials
belong in platform secrets. Do not copy a host's database or configuration without
an explicit owner decision. Container state must be owned by UID 10001 and readable
only by the operator; a bind mount needs those permissions provisioned first.

For an explicitly provisioned user profile named `personal`, provide private files
through `TELELOOM_PERSONAL_API_ID_FILE`, `TELELOOM_PERSONAL_API_HASH_FILE` and
`TELELOOM_PERSONAL_SESSION_FILE`,
and add `compose.user.yaml`:

```text
docker compose -f compose.yaml -f compose.user.yaml up --detach
```

Use the same files for every subsequent Compose command. Other profile IDs use
their normalized uppercase credential prefix. Provision bot-token mounts in the
same way. Permission changes use the stopped-owner CLI inside the container and
remain separate from credentials. Once connected, run the same `profiles_list`
→ exact `chat_resolve` → `messages_get(limit=10)` sequence through the bridge.
A policy denial needs an owner decision for that exact chat; changing package or
backend does not widen access. Doctor success alone is not a Telegram read.

An MCP client can connect over stdio to the running owner with this command and
argument list (use an absolute Compose file path):

```json
{
  "command": "docker",
  "args": ["compose", "-f", "/absolute/path/compose.yaml", "exec", "-T", "teleloom", "python", "/opt/teleloom-container.py", "mcp"]
}
```

The bridge reads the same platform mounts. There is no token in client configuration.
For an override, include its second `-f` argument too. Stop with `docker compose down`;
do not add `--volumes` unless the owner intends to delete persistent evidence.

## Reproducible checks

```text
uv run pytest tests/test_deployment.py
uv run python scripts/check_build_metadata.py --wheel-dir dist
uv run python scripts/check_installed_package.py --wheel-dir dist
uv run python scripts/smoke_container.py --image teleloom-local
```

The container smoke creates its own disposable volume, starts two real stdio
owners, checks discovery and provenance, and verifies persistent configuration.
It uses no accounts or external APIs and removes only its own volume. The optional
manual [CI deployment check](test-loop.md#ci-runner-operations) runs it on the Linux
self-hosted runner. A missing or stopped Docker engine leaves this check
unperformed locally; Compose configuration validation alone is not an image
execution check. Live Telegram, paid transcription and desktop-host acceptance
are separate, explicitly authorized checks.
