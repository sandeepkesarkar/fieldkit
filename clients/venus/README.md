# Venus — FieldKit OpenAI-Backed Demo Client

**Client:** Venus (Demo) — reference only, not a real engagement
**Industry:** General / Reference
**Location:** N/A
**Status:** In Progress — scaffolding (issue #12)
**Deployment:** Hermes Agent supervisor (Mac Mini), model inference routed to the cloud
**AI Provider:** OpenAI, via Hermes's single default profile when venus is the installed client — see [Provider Configuration](#provider-configuration) below

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

This fieldkit install runs exactly one client at a time (issue #61,
superseding an earlier per-client-Hermes-profile design that was the root
cause of issue #59). Venus's model calls go to OpenAI only while venus is
the *installed* client — Hermes's single default profile takes on venus's
provider, key, bot token, and skill dirs for as long as venus stays
installed, and loses them the moment a different client is installed. See
[`platform/docs/hermes/09-per-client-model-profiles.md`](../../platform/docs/hermes/09-per-client-model-profiles.md)
for the full mechanism.

| Setting | Value |
|---|---|
| `model.provider` | `openai-api` (direct OpenAI API key — **not** bare `openai`, which silently aliases to the OpenRouter aggregator, and **not** `openai-codex`, which is OAuth ChatGPT/Codex-subscription auth) |
| `model.default` | `gpt-5.5` (curated default at time of writing — confirm current availability with `hermes model` before first use; the picker's list drifts) |
| Credential | `OPENAI_API_KEY`, installed into Hermes's default profile `.env` by `install_client.sh` from `HERMES_PROVIDER_API_KEY` below |

This is a per-client configuration choice, not code: `platform/photo-agent/`
scripts never call a model API directly (see the Architecture section) — the
provider only matters for the Hermes-mediated slash commands
(`/process_photos`, `/photo_approve`, `/photo_reject`), which is exactly why Story 2's
acceptance scenario ("identical skill behavior across providers") holds: the
skill instructions in `platform/photo-agent/skills/*/SKILL.md` are byte-identical
regardless of which client is installed when they run.

**Setup checklist for a human to complete before venus's model calls can go
live:**

1. Fill in `clients/venus/src/photo-agent/.env` from `.env.example` —
   including the "Hermes gateway install" section at the bottom:
   `TELEGRAM_ALLOWED_USERS` (the admin's Telegram user ID), and
   `HERMES_MODEL_PROVIDER=openai-api`, `HERMES_MODEL_DEFAULT=gpt-5.5`,
   `HERMES_PROVIDER_API_KEY=<venus's OpenAI key>`.
2. Run:
   ```bash
   platform/photo-agent/scripts/install_client.sh venus
   ```
   This writes `CLIENT_NAME=venus` into the repo-root `.env`, and installs
   venus's Telegram bot token/allowlist and OpenAI key into Hermes's
   default profile, sets `skills.external_dirs`, and restarts the gateway.
   It refuses to run (before touching any file) if any required field
   above is missing or blank, or if `HERMES_MODEL_PROVIDER` isn't one it
   recognizes.
3. Verify:
   ```bash
   grep '^CLIENT_NAME=' ~/src/fieldkit/.env        # expect CLIENT_NAME=venus
   hermes doctor                                    # OpenAI API key check should pass
   hermes skills list --source local                # process-photos, photo-approve, photo-reject, all local/enabled
   ```
   Then have the admin's own Telegram account send `/process_photos` (or
   any command) to venus's bot and confirm it's accepted — the real proof
   `TELEGRAM_ALLOWED_USERS` loaded correctly. Hermes's Telegram adapter is
   fail-closed by design, so a missing/wrong allowlist shows up as a
   pairing-code prompt (unset) or total silence (set but wrong) rather than
   open access to anyone — see
   [`09-per-client-model-profiles.md`](../../platform/docs/hermes/09-per-client-model-profiles.md)
   for the full troubleshooting breakdown.

---

## Architecture

Venus reuses `platform/photo-agent/`'s scripts and skills unchanged — the
per-client surface is entirely `clients/venus/src/photo-agent/.env` (secrets,
IDs, and the Hermes gateway install fields above). No client-specific code
exists or is needed.

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

Once `clients/venus/src/photo-agent/.env` is filled in from `.env.example`,
you can exercise the automated e2e rig against venus **without installing it
as the active client** — this is the ad-hoc, single-invocation escape hatch
issue #45/PR #57 built and issue #61 kept: pass `CLIENT_NAME` as an inline
override on each command instead of running `install_client.sh`.
`load_dotenv()`'s default `override=False` means an already-set env var
always wins over the root `.env` file's value (verified empirically), so
this never touches the repo-root `.env` or Hermes's default profile —
whatever client is actually installed (and its live crontab entries) is
completely unaffected:

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
pipeline is provider-agnostic. This inline-override dance is intentionally
scoped to ad-hoc testing only — running venus's *live* Telegram/Hermes
skill dispatch (a real admin typing `/process_photos`) requires actually
installing venus as the active client (see
[Provider Configuration](#provider-configuration) above), not this
override, since Hermes's skill-invoked subprocess never carries it.
