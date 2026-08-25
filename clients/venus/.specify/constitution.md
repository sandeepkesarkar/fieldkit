# Venus — Project Constitution

## Introduction

This constitution establishes the governing principles for the `venus` demo
client's automation system. Venus is a reference implementation (like
`_demo`), not a real client engagement — its purpose is to prove FieldKit's
OpenAI-backed provider path end-to-end. Where this document doesn't override
a principle, the framework constitution (`/.specify/memory/constitution.md`)
governs.

**Last Updated:** 2026-08-25
**Status:** Active (demo/reference)
**Deployment Model:** Hermes Agent supervisor process (Mac Mini); model inference routed to the cloud — see the framework constitution's Architecture Constraints
**AI Provider:** OpenAI, via a dedicated Hermes profile (`venus`, `model.provider: openai-api`) — see [`README.md`](README.md#provider-configuration)
**Development Approach:** Mirrors `_demo`'s pipeline exactly; provider is the only variable under test

---

## Core Values

### 1. Customer Privacy & Trust

Same as `_demo` and the framework constitution's Gate 1: no customer-identifying
information in published content, consent required before using any photo.
Venus uses only synthetic clock-frame test data (see `run_e2e_test.py`), so
this gate is satisfied by construction for the demo itself — real client
instances built from this same pipeline must still strip GPS/EXIF/camera
metadata per Gate 1's known gap (tracked at the framework level, not closed
by this feature).

### 2. Cost Governance & Sustainability

Per-token OpenAI API costs are the only variable cost venus introduces beyond
what `_demo` already has (both share the same Hermes supervisor hardware
cost). No hard daily budget cap is configured for this demo client — it is
exercised manually, not on a schedule — consistent with `_demo`'s posture.

### 3. Human Oversight & Quality Control

Unchanged from the framework constitution's Gate 2: every generated video
requires an explicit Telegram Approve/Reject from the admin before any
publish step runs. This is the exact behavior Story 2's acceptance scenario
requires to be identical across providers — verified by using the same
`check-approval` / `process-photos` skills, unmodified, under the `venus`
Hermes profile.

### 4. Operational Priorities

**Phase 1 (current):** Scaffold venus and its OpenAI provider configuration;
defer the live end-to-end run until a human provisions real credentials
(Telegram bots, Drive folder, Facebook page, `OPENAI_API_KEY`) — see the
closing PR's checklist.

**Phase 2 (next):** Live end-to-end verification, run together with the
orchestrator, after both venus (#12) and mercury (#11) land as PRs.

### 5. Data Integrity & Preservation

Same as `_demo`: all client data lives under `clients/venus/data/` and
`clients/venus/logs/` (gitignored, created at runtime), Google Drive is used
only for client-initiated photo uploads per the framework constitution's
"no cloud storage" exception.

---

## Technical Constraints

### Infrastructure Requirements
- Same Mac Mini / Hermes supervisor as every other client on this machine
- A dedicated Hermes profile (`venus`) — see `README.md`
- A dedicated Telegram bot pair (gateway + approval), separate from every
  other client's, per issue #29's offset-race finding

### Hermes Integration
- Runtime: Hermes Agent, profile `venus`
- Model routing: OpenAI (`model.provider: openai-api`) — explicit per-client
  choice per framework constitution Architecture Constraints
- Skills: `platform/photo-agent/skills/process-photos`,
  `platform/photo-agent/skills/check-approval` — unmodified, shared with
  every other client

### Testing Requirements
- Same test suite as every other client (`platform/photo-agent/tests/`,
  `platform/email-agent/tests/`) — client-agnostic by design, since no
  client-specific code exists
- `clients/venus/src/photo-agent/.env.example` is checked for key-parity
  with `_demo`'s, so provider is the only documented difference between them

---

## Decision-Making Framework

Same priority order as the framework constitution:

1. Customer Privacy
2. System Reliability
3. Budget Constraints
4. Human Oversight
5. Operational Priorities
6. Quality Standards
7. Development Efficiency

---

## Acknowledgment

Venus exists to prove a framework capability (per-client provider choice),
not to serve a real customer. All stakeholders agree this client:
- Is exercised manually, not scheduled
- Uses only synthetic/placeholder data
- Is a template that a real OpenAI-backed engagement would copy, not modify
  in place

---

*Established: 2026-08-25*
*Authority: Sandeep Kesarkar (FieldKit)*
*Framework: FieldKit*
*AI Provider: OpenAI, via Hermes profile `venus` (`openai-api`)*
