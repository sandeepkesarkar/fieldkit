# Mercury — FieldKit Demo Customer (Anthropic-backed)

**Client:** Mercury (demo customer — not a real engagement)
**Industry:** General / Reference
**Location:** N/A
**Status:** In Progress — Scaffolded, live credentials pending
**Deployment:** Mac Mini (on-premise, interim — see `platform/.specify/003-hermes-runtime/spec.md`)
**AI Provider:** Anthropic — explicit, not inherited by default. Model calls route through a
dedicated Hermes gateway profile (`mercury`), separate from `_demo`'s profile, so this
customer can run side by side with a future OpenAI-backed demo customer (#12) without either
one's provider choice leaking into the other. See
[`platform/docs/hermes/02-gateway-setup.md`](../../platform/docs/hermes/02-gateway-setup.md)
for the shared-gateway background and the per-client provider question this client resolves.

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

## Hermes profile — required `CLIENT_NAME` (issue #59)

Mercury runs under its own Hermes profile (`hermes -p mercury ...`) — see
[`09-per-client-model-profiles.md`](../../platform/docs/hermes/09-per-client-model-profiles.md)
for the full per-client profile mechanism, and its "Live skill dispatch also
needs `CLIENT_NAME`" section specifically. Beyond the provider/API-key setup
that doc's checklist already covers, `~/.hermes/profiles/mercury/.env`
**must also set `CLIENT_NAME=mercury`** — with no error path if it's
missing. `platform/photo-agent/skills/*/SKILL.md` shell out to the
photo-agent scripts with no inline `CLIENT_NAME` of their own, so
`/process_photos`, `/photo_approve`, and `/photo_reject` typed in mercury's
Telegram bot depend entirely on that value already being set in the
profile's own `.env`; without it, every invocation silently falls back to
the repo root `fieldkit/.env`'s `CLIENT_NAME` (`_demo`) and operates against
`_demo`'s Drive folder, Telegram bot, and Facebook Page instead of
mercury's — confirmed live on 2026-08-26. Verify with
`grep '^CLIENT_NAME=' ~/.hermes/profiles/mercury/.env` (safe to inspect
directly, not a secret) before any live test — see
[`11-manual-e2e-mercury-walkthrough.md`](../../platform/docs/hermes/11-manual-e2e-mercury-walkthrough.md)'s
pre-flight section.

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
