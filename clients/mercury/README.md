# Mercury — FieldKit Demo Customer (Anthropic-backed)

**Client:** Mercury (demo customer — not a real engagement)
**Industry:** General / Reference
**Location:** N/A
**Status:** In Progress — Scaffolded, live credentials pending
**Deployment:** Mac Mini (on-premise, interim — see `platform/.specify/003-hermes-runtime/spec.md`)
**AI Provider:** Anthropic — explicit, not inherited by default. Model calls route through
Hermes's single default profile, installed with mercury's config via
`platform/photo-agent/scripts/install_client.sh mercury` (issue #61 — this fieldkit install
runs exactly one client at a time; see
[`platform/docs/hermes/09-per-client-model-profiles.md`](../../platform/docs/hermes/09-per-client-model-profiles.md)).

---

## Overview

`clients/mercury/` is FieldKit's second reference implementation, created to prove out
per-client model-provider configuration (FR-004/FR-005,
[`platform/.specify/003-hermes-runtime/spec.md`](../../platform/.specify/003-hermes-runtime/spec.md)
Story 2) with a real, working example — not just a spec claim. It mirrors `_demo`'s full
pipeline (video → Telegram approval → Facebook) so the two demo customers are directly
comparable except for provider. It uses clearly fake credentials and placeholder data; it is
not a real client engagement.

Real client implementations live in separate private repositories. See
[`clients/README.md`](../README.md) for the architecture decision behind this.

---

## Installing mercury as the active client

This fieldkit install runs exactly one client at a time (issue #61,
superseding an earlier per-client-Hermes-profile design that was the root
cause of issue #59 — a live skill invocation silently resolving against
`_demo`'s credentials). To make mercury that one active client:

1. Fill in `clients/mercury/src/photo-agent/.env` from `.env.example` —
   including the "Hermes gateway install" section at the bottom
   (`TELEGRAM_ALLOWED_USERS`, `HERMES_MODEL_PROVIDER=anthropic`,
   `HERMES_MODEL_DEFAULT`, `HERMES_PROVIDER_API_KEY`).
2. Run:
   ```bash
   platform/photo-agent/scripts/install_client.sh mercury
   ```
   This writes `CLIENT_NAME=mercury` into the repo-root `.env`, installs
   mercury's Telegram bot token/allowlist and Anthropic key into Hermes's
   default profile, points `skills.external_dirs` at
   `platform/photo-agent/skills`, and restarts the gateway.
3. Verify: `grep '^CLIENT_NAME=' ~/src/fieldkit/.env` should read
   `CLIENT_NAME=mercury`, and `hermes skills list --source local` should
   show `process-photos`, `photo-approve`, `photo-reject`.

Full mechanism, provider-identity notes (`openai-api` vs. bare `openai` vs.
`openai-codex`), and what to do about a leftover pre-#61 Hermes profile:
[`platform/docs/hermes/09-per-client-model-profiles.md`](../../platform/docs/hermes/09-per-client-model-profiles.md).

---

## Feature Status

| Feature | Spec | Clarify | Plan | Build | Live |
|---------|------|---------|------|-------|------|
| Photo-approval e2e cycle (Anthropic-backed) | — | — | — | ✅ Scaffolded | ⏳ Deferred — see PR checklist |

Live end-to-end verification (trigger → photo processing → Telegram approval → confirmed
post) is deliberately **not** run as part of scaffolding this client. Everything needed to
run it is in place (client directory, `.env.example`, e2e test rig reuse via
`platform/photo-agent/scripts/run_e2e_test.py`); the live run itself is a separate, deferred
step done with the human once real credentials are provisioned.

---

## Governance

All decisions are governed by the project constitution:
→ [`.specify/constitution.md`](.specify/constitution.md)

Platform-level engine specs:
→ [`platform/.specify/002-photo-agent/spec.md`](../../platform/.specify/002-photo-agent/spec.md)
→ [`platform/.specify/003-hermes-runtime/spec.md`](../../platform/.specify/003-hermes-runtime/spec.md)

---

## Development Workflow

| Step | Status |
|------|--------|
| Constitution | ✅ |
| Specification | ⏳ |
| Clarification | ⏳ |
| Technical Planning | ⏳ |
| Task Breakdown | ⏳ |
| Implementation | ✅ Scaffolded (reuses `platform/photo-agent` engine, no client-specific code) |
| Production | ⏳ |

---

## Next Steps

1. [x] Fill in constitution
2. [x] Scaffold client directory and `.env.example` from `_template`/`_demo` convention
3. [ ] Provision live credentials (see PR description for the exact checklist)
4. [ ] Run one live end-to-end photo-approval cycle with the human
5. [ ] Run `/speckit.specify` if/when this client grows beyond the reference e2e cycle
