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
| Telegram bot for Hermes commands | Its own `TELEGRAM_BOT_TOKEN` (see `.env.example`) — must be a separate BotFather bot from every other client's, and separate from `TELEGRAM_APPROVAL_BOT_TOKEN` below, for the same offset-race reason documented in issue #29 |

This is a per-client configuration choice, not code: `platform/photo-agent/`
scripts never call a model API directly (see the Architecture section) — the
provider only matters for the Hermes-mediated slash commands
(`/process_photos`, `/check_approval`), which is exactly why Story 2's
acceptance scenario ("identical skill behavior across providers") holds: the
skill instructions in `platform/photo-agent/skills/*/SKILL.md` are byte-identical
regardless of which client's profile executes them.

**Setup checklist for a human to complete before venus's model calls can go
live** (deliberately not run by the orchestrator — see the PR description):

1. `hermes profile create venus`
2. `hermes -p venus config set model.provider openai-api`
3. `hermes -p venus config set model.default gpt-5.5` (or whichever current model `hermes -p venus model` shows for `openai-api`)
4. Put `OPENAI_API_KEY=...` in `~/.hermes/profiles/venus/.env`
5. `hermes -p venus doctor` — confirm it reports the OpenAI API key as configured (no more `✗ model.provider 'openai-api' is set but no API key is configured`)
6. `hermes -p venus gateway install --start-now --start-on-login` — a second, independently-supervised gateway process bound to venus's own `TELEGRAM_BOT_TOKEN`

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

```bash
# fieldkit/.env
CLIENT_NAME=venus

cd platform/photo-agent
python3 scripts/run_e2e_test.py --duration 20
```

This is the same rig `_demo` uses (`platform/photo-agent/scripts/run_e2e_test.py`)
— nothing venus-specific was needed there, which is itself evidence the
pipeline is provider-agnostic. See the PR description for the exact list of
live credentials this requires that don't exist yet.
