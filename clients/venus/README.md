# Venus — FieldKit OpenAI-Backed Demo Client

**Client:** Venus (Demo) — reference only, not a real engagement
**Industry:** General / Reference
**Location:** N/A
**Status:** In Progress — scaffolding (issue #12)
**Deployment:** Hermes Agent supervisor (Mac Mini), model inference routed to the cloud
**AI Provider:** OpenAI, via a dedicated Hermes profile — see [Provider Configuration](#provider-configuration) below

---

## Overview

`venus` is the second of FieldKit's two demo clients proving FR-004 of
`platform/.specify/003-hermes-runtime/spec.md`: that OpenAI is supported as an
explicit per-client provider choice, with no behavioral difference in the
skills themselves versus the Anthropic-backed path. It uses the same
photo-agent pipeline as `_demo` (video generation → Telegram approval →
Facebook publish), backed by fake/placeholder credentials and data. It is not
a real client engagement.

Its sibling, `mercury` (issue #11), is the Anthropic-backed counterpart. The
two clients are structurally identical — same pipeline, same skills, same
`.env.example` shape — so that a side-by-side diff of their
[Provider Configuration](#provider-configuration) sections is the whole story
of what changes between providers.

Real client implementations live in separate private repositories. See
[`clients/README.md`](../README.md) for the architecture decision behind this.

---

## Provider Configuration

Venus runs under its own Hermes profile so its model calls go to OpenAI while
the default profile (bound to `_demo`) keeps calling Anthropic — see
[`platform/docs/hermes/09-per-client-model-profiles.md`](../../platform/docs/hermes/09-per-client-model-profiles.md)
for the verified mechanism and why this replaces the single-global-config
approach originally sketched in `02-gateway-setup.md`.

| Setting | Value |
|---|---|
| Hermes profile name | `venus` |
| `model.provider` | `openai-api` (direct OpenAI API key — **not** bare `openai`, which silently aliases to the OpenRouter aggregator, and **not** `openai-codex`, which is OAuth ChatGPT/Codex-subscription auth) |
| `model.default` | `gpt-5.5` (curated default at time of writing — confirm current availability with `hermes -p venus model` before first use; the picker's list drifts) |
| Credential | `OPENAI_API_KEY`, in the `venus` profile's own `~/.hermes/profiles/venus/.env` (never in this repo) |
| Telegram bot for Hermes commands | Its own `TELEGRAM_BOT_TOKEN` (see `.env.example`) — must be a separate BotFather bot from every other client's. A single bot now handles both Hermes's gateway traffic and the photo-approval flow — issue #49 retired the second, dedicated approval bot (`TELEGRAM_APPROVAL_BOT_TOKEN`) that issue #29 originally required; see `platform/docs/hermes/10-text-based-approval-migration.md` |
| Telegram admin authorization | `TELEGRAM_ALLOWED_USERS` set to the admin's Telegram user ID (same value as `ADMIN_TELEGRAM_CHAT_ID` in `.env.example`) — a fresh profile has **no** allowlist until this is set, same as the bot token below |

This is a per-client configuration choice, not code: `platform/photo-agent/`
scripts never call a model API directly (see the Architecture section) — the
provider only matters for the Hermes-mediated slash commands
(`/process_photos`, `/photo_approve`, `/photo_reject`), which is exactly why Story 2's
acceptance scenario ("identical skill behavior across providers") holds: the
skill instructions in `platform/photo-agent/skills/*/SKILL.md` are byte-identical
regardless of which client's profile executes them.

**Setup checklist for a human to complete before venus's model calls can go
live** (deliberately not run by the orchestrator — see the PR description).
Note that every credential below goes in the **profile's own**
`~/.hermes/profiles/venus/.env` — a fresh profile does not read
`clients/venus/src/photo-agent/.env` at all; that file is for the plain
Python cron scripts (`process_photos.py`, `check_approval.py`, ...), which
are a completely separate consumer from the Hermes gateway process:

1. `hermes profile create venus`
2. `hermes -p venus config set model.provider openai-api`
3. `hermes -p venus config set model.default gpt-5.5` (or whichever current model `hermes -p venus model` shows for `openai-api`)
4. Put `OPENAI_API_KEY=...`, `TELEGRAM_BOT_TOKEN=...` (venus's gateway bot, from the checklist's Telegram step), `TELEGRAM_ALLOWED_USERS=...` (the admin's Telegram user ID), **and `CLIENT_NAME=venus`** in `~/.hermes/profiles/venus/.env`. The gateway reads its bot token and its authorization allowlist from the active profile's own env, not from this repo — an install without this step would create a profile that is correctly configured for OpenAI but binds to no Telegram bot at all, or (if only the token is set and not the allowlist) leaves the admin themselves unrecognized (see step 7's note — this is a fail-closed system, not fail-open).
   **`CLIENT_NAME=venus` is not optional and has no error path if it's missing** (issue #59): `platform/photo-agent/skills/*/SKILL.md` shell out to the photo-agent scripts with no inline `CLIENT_NAME` of their own, so `/process_photos`, `/photo_approve`, and `/photo_reject` typed in venus's Telegram bot would otherwise silently fall back to the repo root `fieldkit/.env`'s `CLIENT_NAME` (`_demo`) and operate against `_demo`'s Drive folder, Telegram bot, and Facebook Page instead of venus's — exactly what happened live against `mercury` before this was fixed. See [`09-per-client-model-profiles.md`](../../platform/docs/hermes/09-per-client-model-profiles.md#live-skill-dispatch-also-needs-client_name) for the verified mechanism.
   Verify both values actually landed — safe to inspect directly, neither is a secret:
   ```bash
   grep TELEGRAM_ALLOWED_USERS ~/.hermes/profiles/venus/.env
   grep CLIENT_NAME ~/.hermes/profiles/venus/.env
   ```
   **Do not** rely on the absence of Hermes's "No env user allowlists configured" startup warning as proof `TELEGRAM_ALLOWED_USERS` is set: that check is `any(os.getenv(v) for v in <~20 platform allowlist vars>)` across every supported platform (`gateway/run.py`) — it's suppressed by *any one* of them being set, so its absence tells you nothing about `TELEGRAM_ALLOWED_USERS` specifically. There is no equivalent startup warning for a missing `CLIENT_NAME` at all — the `grep` above is the only check.
5. `hermes -p venus config set skills.external_dirs '["~/src/fieldkit/platform/photo-agent/skills"]'` — profiles have fully isolated skill discovery (see `docs/design/profile-builder.md` in the Hermes source tree), so without this, `/process_photos`, `/photo_approve`, and `/photo_reject` are invisible to venus's gateway even though they're already registered for the default profile (`03-process-photos-skill.md`, `10-text-based-approval-migration.md`). Confirm with `hermes -p venus skills list --source local` — expect `process-photos`, `photo-approve`, and `photo-reject` all listed as `local` / `enabled`.
6. `hermes -p venus doctor` — confirm it reports the OpenAI API key as configured (no more `✗ model.provider 'openai-api' is set but no API key is configured`). **`doctor` does not check the Telegram allowlist at all** — use the `grep` from step 4 for that.
7. `hermes -p venus gateway install --start-now --start-on-login` — a second, independently-supervised gateway process bound to venus's own `TELEGRAM_BOT_TOKEN`. Then have the admin's own Telegram account send `/process_photos` (or any command) to venus's bot and confirm it's accepted — that's the real proof `TELEGRAM_ALLOWED_USERS` loaded correctly for *this* platform.
   **What actually happens if step 4 is skipped or wrong isn't "anyone can now command the bot"** — Hermes's Telegram adapter is fail-closed by design (`plugins/platforms/telegram/adapter.py`: "Fail-closed: no allowlist means deny by default"). But which fail-closed symptom the admin sees depends on *which* mistake was made — these are two different troubleshooting experiences, not one generic "misconfigured" outcome (traced in `gateway/authz_mixin.py::_get_unauthorized_dm_behavior`, rules 5–6):
   - **`TELEGRAM_ALLOWED_USERS` left unset entirely** (step 4 skipped): no allowlist exists at all, so Hermes falls back to its open-gateway default — the admin's DM gets a **pairing-code prompt** instead of a normal response. Expect a pairing flow you didn't ask for, not silence.
   - **`TELEGRAM_ALLOWED_USERS` set but wrong/mistyped** (e.g. the wrong chat ID): an allowlist *does* exist, which Hermes takes as a deliberate access restriction — unauthorized senders are **silently ignored**, no pairing prompt, no response, nothing. Expect total silence, not an error.

   Either way, no unauthenticated third party ever gains access — the one way this becomes an actual open-access exposure is `GATEWAY_ALLOW_ALL_USERS=true` or a platform-specific `*_ALLOW_ALL_USERS`/open `dm_policy`, none of which this checklist sets.

---

## Architecture

Venus reuses `platform/photo-agent/`'s scripts and skills unchanged — the
per-client surface is entirely `clients/venus/src/photo-agent/.env` (secrets
and IDs) plus the Hermes profile above (model provider). No client-specific
code exists or is needed.

```
clients/venus/
├── README.md                      ← this file
├── .specify/
│   └── constitution.md            ← client principles (per FieldKit framework constitution)
└── src/photo-agent/
    └── .env.example                ← copy to .env and fill in before running any script
```

`data/` and `logs/` are created at runtime (gitignored) — see
`FIELDKIT_DATA_DIR` / `FIELDKIT_LOG_DIR` in `.env.example`.

---

## Feature Status

| Feature | Spec | Clarify | Plan | Build | Live |
|---------|------|---------|------|-------|------|
| Photo-approval e2e cycle (issue #12) | ✅ (spec 003, Story 2) | — | — | ✅ scaffolded | ⏳ deferred — see PR description |

---

## Governance

All decisions are governed by the project constitution:
→ [`.specify/constitution.md`](.specify/constitution.md)

Platform-level engine spec:
→ [`platform/.specify/003-hermes-runtime/spec.md`](../../platform/.specify/003-hermes-runtime/spec.md)

---

## Running the end-to-end test

Once the [Provider Configuration](#provider-configuration) checklist above is
complete and `clients/venus/src/photo-agent/.env` is filled in from
`.env.example`:

**Do not set `CLIENT_NAME=venus` in the shared `fieldkit/.env`.** That file
is read by every client's scripts on this machine — including the live
crontab entries that (as of this writing, pre-migration — see
`platform/docs/hermes/10-text-based-approval-migration.md`) still run
`check_approval.py --source cron` and `upload_facebook.py --source cron`
every minute against `_demo` right now
(see `platform/docs/hermes/05-cron-verification.md`). Permanently editing it
would silently redirect that live cron traffic at venus's credentials/data
until someone edits it back. Instead, pass `CLIENT_NAME` as an inline
override on each command — `load_dotenv()`'s default `override=False` means
an already-set env var always wins over the root `.env` file's value
(verified empirically against the installed `python-dotenv==1.2.2`), so this
is safe to do without touching the shared file at all:

```bash
cd platform/photo-agent

# Prerequisite check (process_photos.py shells out to both):
which ffmpeg || echo "MISSING — brew install ffmpeg"
which gws || echo "MISSING — see platform/photo-agent/docs for gws setup"

CLIENT_NAME=venus FIELDKIT_ROOT=/absolute/path/to/fieldkit \
    python3 scripts/run_e2e_test.py --duration 20
```

While that's running, Stage 4 needs something to actually process the
approval. Issue #49 removed the inline Approve/Reject buttons and the
cron-based polling loop that used to watch for a button tap — reply
`/photo_approve` (or `/photo_reject`) to venus's bot in Telegram, or, to
approve without a live Hermes gateway running, invoke the decision directly
in another terminal, with the same inline override:

```bash
# Terminal 2, once Stage 3 has sent the approval-request message:
CLIENT_NAME=venus FIELDKIT_ROOT=/absolute/path/to/fieldkit \
    python3 scripts/check_approval.py --callback-data approve

# Terminal 3, once Stage 4 passes, until Stage 5 reports the post is live:
CLIENT_NAME=venus FIELDKIT_ROOT=/absolute/path/to/fieldkit \
    python3 scripts/upload_facebook.py --source cron
```

(`upload_facebook.py`'s own cron leg is unaffected by issue #49 — only the
photo-approval flow's polling was retired, not the separate Facebook-upload
pipeline.)

This is the same rig `_demo` uses (`platform/photo-agent/scripts/run_e2e_test.py`)
— nothing venus-specific was needed there, which is itself evidence the
pipeline is provider-agnostic. See the PR description for the exact list of
live credentials this requires that don't exist yet, and for a flagged
follow-up: this inline-override dance is a one-off-test workaround, not a
real fix for running two clients' cron/gateway processes permanently
alongside each other on one machine.
