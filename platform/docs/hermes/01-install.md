# Hermes Agent — Install Notes (Mac Mini / `servicehub-dev`)

Covers issue #5 only: getting the `hermes` CLI installed and on `PATH`. Gateway
configuration, model provider setup, and skill dispatch are separate issues
(#6, #7, #8) — this note stops at a clean `hermes doctor` baseline.

Source: [`platform/.specify/003-hermes-runtime/spec.md`](../../.specify/003-hermes-runtime/spec.md) (FR-001, Constraints).

## What was run

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- \
  --skip-setup \
  --skip-browser \
  --skip-computer-use \
  --non-interactive
```

Before running, the script was downloaded and read in full rather than piped
directly to `bash` — it's a ~3,500-line installer from a third party, and this
runs on a machine other services depend on.

Flag choices, and why:

- `--skip-setup` — skips the interactive LLM-provider wizard. Provider/model
  config is issue #6's scope, not this one.
- `--skip-browser` / `--skip-computer-use` — skips Playwright/Chromium and the
  `cua-driver` computer-use tooling. This box runs a Telegram gateway, not
  browser automation; installing GUI-automation drivers on a headless service
  host is unnecessary surface area.
- `--non-interactive` — no prompts; safe to background.

**Not run:** `hermes claw migrate`. Per spec FR-008, no OpenClaw state
(`~/.openclaw`) is carried over — confirmed after install that
`~/.openclaw/openclaw.json`'s mtime was unchanged and nothing under `~/.hermes`
references OpenClaw, ClawdBot, or MoltBot.

## Prerequisites (installer-managed)

The installer detects and installs anything missing into `~/.hermes/` rather
than touching system Python/Node — it does not shadow or upgrade whatever
`python3`/`node` already resolve to on `PATH` elsewhere on this machine.

| Tool | Result |
|---|---|
| `uv` | Installed to `~/.hermes/bin` (0.12.3) |
| Python 3.11 | Not previously present; installed via `uv` (3.11.15) |
| Git | Already present (2.50.1) — used as-is |
| Node.js | System npm (11.12.1) couldn't honor the repo's `.npmrc`
  (`min-release-age-exclude` needs npm 11.10–11.16 but excludes 11.12); installer
  fell back to its own managed Node 22 (22.23.2) in `~/.hermes/node/` |
| ripgrep | Already present (15.1.0) — used as-is |
| ffmpeg | Already present (8.1.1) — used as-is |

## Quirks encountered

1. **SSH clone attempted first, fell back to HTTPS automatically.** No SSH key
   configured for `github.com` under this account for the `hermes-agent`
   clone; the installer's fallback handled it without intervention.
2. **`uv.lock` resolution failed under `--locked`, fell back to a plain PyPI
   resolve.** The bundled lockfile didn't match the current `uv` exclude-newer
   settings (`error: The lockfile at uv.lock needs to be updated, but --locked
   was provided`). The installer's fallback tier (`uv pip install -e '.[all]'`
   against PyPI directly) succeeded; `hermes doctor` reports no functional gap
   from this, only a note that a lockfile bump would clear two npm build-tool
   advisories in the (skipped) browser/TUI workspaces.

Neither quirk needed manual fixing — both are handled by fallbacks already
built into the installer.

## Verification

```
$ hermes --version
Hermes Agent v0.20.1 (2026.8.13)
Install directory: /Users/sandeep_a_k/.hermes/hermes-agent
Python: 3.11.15
OpenAI SDK: 2.24.0

$ hermes doctor
...
Found 4 issue(s) to address:
  1. Run 'hermes doctor --fix' or 'hermes setup' to migrate config
  2. web workspace has 3 npm vulnerabilities         (build-tool only; browser tools were skipped)
  3. ui-tui workspace has 3 npm vulnerabilities       (build-tool only; browser tools were skipped)
  4. Run 'hermes setup' to configure missing API keys for full tool access
```

All four `doctor` findings are expected at this stage: no model provider is
configured yet (issue #6), and the npm advisories live in workspaces this
install explicitly skipped (`--skip-browser`). `hermes --version` succeeds and
the CLI is on `PATH` via `~/.local/bin/hermes`, which was already on `PATH` —
no shell profile edit was needed.

## Install locations

| What | Where |
|---|---|
| CLI launcher | `~/.local/bin/hermes` (+ `hermes-agent`, `hermes-acp`) |
| Config | `~/.hermes/config.yaml` |
| Secrets | `~/.hermes/.env` |
| Data (cron, sessions, logs) | `~/.hermes/cron/`, `~/.hermes/sessions/`, `~/.hermes/logs/` |
| Source checkout | `~/.hermes/hermes-agent/` |
| Bundled skills | `~/.hermes/skills/` (81 synced; unrelated to FieldKit's own `SKILL_*.md` dispatch, which issues #7/#8 port separately) |

## Next steps (separate issues)

- #6 — configure the Telegram gateway + supervisor and default model provider
  (`hermes setup`, `hermes gateway install`)
- #7 / #8 — port `process_photos` / `check_approval` dispatch as Hermes skills
- #14 — uninstall OpenClaw once Hermes fully covers its role (SC-003)
