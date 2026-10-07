# Set up Teleloom

Teleloom is a toolkit of Telegram tools, portable agent skills, an MCP server and
the `teleloom` CLI. [Русская инструкция](install-ru.md).

## Install

Install Git and [uv](https://docs.astral.sh/uv/getting-started/installation/).
Python 3.12–3.14 is supported; uv can provision a supported interpreter.

```sh
git clone --branch v0.5.0 --single-branch https://github.com/starsinc1708/teleloom.git
cd teleloom
uv sync --frozen
uv run teleloom init
uv run teleloom doctor --mode local
```

The clone is a locked, editable source installation (`build_type=dev`). Verified
noneditable wheel/sdist files are available in [Releases](https://github.com/starsinc1708/teleloom/releases).
The CLI, module, package and private application directory are named `teleloom`.
Credentials use the `teleloom` OS keyring namespace; environment overrides use
`TELELOOM_*`. A different application namespace is a separate installation: do
not point it at another running owner or replace that owner without a deliberate
migration. Authenticate new profiles locally.

On Windows, if Git/uv are absent:

```powershell
winget install --id Git.Git -e
winget install --id astral-sh.uv -e
```

Optional extras are installed only as needed, for example `uv sync --frozen --extra pdf`.
OCR also needs a local Tesseract installation. Local transcription uses optional
engines/model files; see [attachments](attachments.md) before choosing a provider.

## Connect an account or bot

For a user account, obtain your own API ID/hash at [my.telegram.org](https://my.telegram.org).
Enter credentials in the local hidden prompts:

```sh
uv run teleloom auth user --profile personal --ui browser
uv run teleloom profile list
```

Scan the QR from **Telegram → Settings → Devices → Link Desktop Device**.
Use `--ui terminal` for terminal QR or `--method code` for the code flow.
The temporary browser page handles 2FA locally. Do not share its URL, QR,
password, login code or session with an agent. Saving new sessions requires a
working OS credential backend; headless hosts need secure keyring provisioning.
There is no plaintext credential fallback and `.env` is not automatically loaded.

For a bot:

```sh
uv run teleloom auth bot --profile helper
```

Enter its token locally. Bot polling is off initially. Enable it explicitly when
needed; an existing webhook/polling consumer is reported as a conflict and is
preserved. Bot API history is limited to collected observations. See
[the reference](reference.md) for bot polling and explicit bot MTProto setup.

## Restrict access

New profiles default to `read_mode=all`: all chats available to that account.
Start with read-only tools, then choose a bounded selection of chats.

```sh
uv run teleloom stop
uv run teleloom config exposure --mode read-only
uv run teleloom config client --client codex
uv run teleloom skills install --client codex
```

After configuring your client, use `chats_list`/`chat_resolve` to obtain exact
canonical IDs. Restrict the profile while the owner is stopped:

```sh
uv run teleloom stop
uv run teleloom profile allow --scope read -- personal CHAT_ID
uv run teleloom profile read-policy personal --mode selected
```

Replace `CHAT_ID` with the chosen canonical ID. `--` allows negative IDs.
A selected policy with no allowed chats denies reading all chats. Tools and chat
access are independent gates; send, mutation, broadcast, sync, AI, event and
transcription grants are separate. Restart/reconnect after changing configuration.
Skills provide instructions and never grant permissions.

## Connect your client

`teleloom config client --client CLIENT` prints a fragment with the exact Python
interpreter and data directory. Merge only its `teleloom` entry into your client's
configuration, retaining other settings. No owner token is printed.

Supported targets: `codex`, `claude`, `opencode` (v2), `opencode-v1`, `hermes`, `pi`.
Use the matching target for `teleloom skills install`. On Windows GUI connections
use `pythonw.exe` when available. The bridge starts the one local owner on demand.
[Exact files and native discovery commands](clients.md).

## First read

Ask the connected agent:

> Check `server_status`, list profiles and explain their read policies. Ask me to
> choose a profile. List up to ten accessible chats and ask me to choose one.
> Read its latest 20 messages without acknowledging them. Give a short summary
> with message links and explicit gaps. Do not send or modify anything.

Then run `uv run teleloom doctor --mode mcp` from the same environment. It inspects
the already-running owner and catalog without reading Telegram or calling AI.
Compare package version, source commit and build type; version alone does not
distinguish a release wheel from editable source.

## If something fails

| Symptom | Next step |
|---|---|
| Cannot save a session | Configure a secure OS credential store; repeat local auth after it works |
| No tools / stale tools | Check the generated interpreter and `TELELOOM_DATA_DIR`, run MCP doctor, reconnect the client |
| Access denied | Inspect profile read policy and the operation's exact grant; change grants locally only if intended |
| Bot sees no old messages | Bot API cannot fetch arbitrary historical messages; inspect collected coverage and polling state |
| Delivery is unknown | Inspect Telegram and the receipt; automatic replay is intentionally disabled |
| OpenCode's first list is empty | Keep its service running through asynchronous startup and check `opencode mcp list` again |

## Existing installations and upgrades

Keep the installed interpreter, credential backend, profiles, data directory and
client configuration. Use that interpreter directly for diagnostics; `uv sync`
in an owner environment can replace its noneditable package. The
[release upgrade procedure](release-pipeline.md#selecting-and-preserving-an-owner)
preserves state and verifies the exact downloaded artifact. A product rename
alone does not require an owner upgrade.
