# Mercury — Project Constitution

## Introduction

This constitution governs the Mercury demo customer — FieldKit's second reference
implementation, created alongside `_demo` to validate per-client model-provider
configuration. All values marked `[DEMO]` are placeholders — replace with real values when
onboarding an actual client. This document otherwise mirrors `_demo`'s constitution; see
[`clients/_demo/.specify/constitution.md`](../../_demo/.specify/constitution.md) for the
sibling reference.

**Last Updated:** 2026-08-25
**Status:** Reference / Scaffolded — live credentials pending
**Deployment Model:** Mac Mini (on-premise, interim — see `platform/.specify/003-hermes-runtime/spec.md`)
**AI Provider:** Hermes Agent (self-hosted runtime); **explicitly Anthropic-backed**, routed
through a dedicated `mercury` Hermes gateway profile (not the shared/default profile `_demo`
uses) — see Hermes Integration below.
**Development Approach:** Phased implementation

---

## Deployment Architecture

### Mac Mini Model (interim)

**Hosting & Ownership:**
- FieldKit provides Mac Mini for development and deployment, shared with `_demo` during the
  interim development period (both demo customers currently run on the same physical box)
- A production client deployment runs exactly one client per installation — this
  shared-box arrangement is a dev/demo-only artifact, not the target production topology
- Upon a real client engagement reaching completion, hardware/cloud handoff follows the same
  model as `_demo` — see `platform/.specify/003-hermes-runtime/spec.md` for the Mac Mini →
  Cloud pivot roadmap

**Implications:**
- AI inference routes to Anthropic's API via Hermes Agent, through the `mercury` gateway
  profile specifically — kept separate from `_demo`'s profile so the two demo customers can
  validate genuinely different providers concurrently without cross-talk
- Non-AI data storage/ownership model follows the same not-yet-finalized post-pivot state as
  `_demo` (see `_demo`'s constitution for the caveat)

---

## Core Values

### 1. Customer Privacy & Trust

**Principle:** Customer data and job-site information must never appear in public-facing
content without explicit consent.

**Implementation Requirements:**
- [x] All metadata (GPS, timestamps, camera info) stripped from photos before posting
- [x] Human verification required for all customer-facing content
- [x] All customer data stored locally on Mac Mini (not cloud) `[DEMO]`

---

### 2. Cost Governance & Sustainability

**Principle:** AI spending must be predictable, controlled, and sustainable for a small
business.

**Budget Constraints:**
- **Hard Daily Limit:** $5.00 USD per day `[DEMO]`
- **Alert Threshold:** 75% of daily budget consumed
- **Enforcement:** System automatically pauses AI operations when daily limit reached

**Hermes Cost Model:**
- Self-hosted Hermes Agent supervisor process (one-time hardware cost), running its own
  `mercury` gateway profile separate from `_demo`'s
- Per-token Anthropic API costs
- External API costs only for other services that can't be self-hosted

**Priority During Budget Constraints:**
1. Telegram approval flow (must always run)
2. Facebook posting

---

### 3. Human Oversight & Quality Control

**Principle:** AI assists human judgment; it does not replace it. All customer-facing content
requires human review.

**Approval Requirements:**
- ALL customer-facing content requires admin approval before publication
- Social media posts (captions, images/video)

**Approval Workflow:**
- Agent sends draft to admin via Telegram
- Admin can approve, request revisions, or reject
- System tracks approval history locally

---

### 4. Operational Priorities

**Phase 1 (Current):** Photo-approval e2e cycle, Anthropic-backed
- **Goal:** One full trigger → photo processing → Telegram approval → confirmed-post cycle,
  verified live with a human, proving the per-client provider config works end to end
- **Priority:** This client exists specifically to validate FR-004/FR-005; scope stays
  narrow until that's proven

---

### 5. Data Integrity & Preservation

**Principle:** Business data is valuable and should be preserved. Local storage ensures
control.

**Data Storage:**
- All data stored on Mac Mini, under `clients/mercury/data` and `clients/mercury/logs`
- No cloud storage except Gmail (email), Google Drive (photo intake), and Telegram
  (notifications)
- Data ownership: client owns all data on their Mac Mini `[DEMO]`

**External Services Used:**
- [x] Gmail (agent email inbox) `[DEMO]`
- [x] Google Drive (photo intake) `[DEMO]`
- [x] Telegram (admin notifications and commands) `[DEMO]` — dedicated bot, separate from
  `_demo`'s and `_construction_co`'s, per the existing convention (see `.env.example`)
- [x] Facebook (posting) `[DEMO]`

---

## Feature-Specific Principles

### Social Media Management

**Philosophy:** Every post must look professionally crafted and never expose customer
privacy.

**Key Principles:**
- Before/after format showcases work quality without identifying customers
- All metadata stripped from photos before any processing
- No post goes live without admin approval

**Platforms:** Instagram, Facebook `[DEMO — mirrors _demo's scope]`

---

## Technical Constraints

### Infrastructure Requirements
- Mac Mini M-series (shared with `_demo` during the interim dev period)
- Reliable internet connection (always-on)
- Dedicated Gmail account for agent email (`agent@[DEMO].com`)
- macOS for deployment

### Hermes Integration

> **Superseded by issue #61 — the dedicated-profile design described below
> is retired, not current.** This project now runs exactly one client at a
> time via Hermes's single DEFAULT profile only, switched with
> `platform/photo-agent/scripts/install_client.sh mercury` — see
> [`platform/docs/hermes/09-per-client-model-profiles.md`](../../../platform/docs/hermes/09-per-client-model-profiles.md)
> for the current, authoritative mechanism. The paragraphs below are left
> as historical record of the original design (and the real, verified
> Hermes CLI behavior they documented remains accurate as CLI behavior);
> they do not describe how mercury is actually configured today.

- Self-hosted Hermes Agent runtime on Mac Mini
- Model calls route to **Anthropic's API only** — this is explicit, not inherited from a
  repo-wide default
- Verified live against the installed Hermes CLI (v0.20.5): profile targeting uses
  `hermes -p mercury <command>` (undocumented but confirmed real — scopes a single
  invocation to that profile, leaves the global sticky default untouched), after a one-time
  `hermes profile create mercury`. `hermes profile use mercury` (a separate, sticky
  global-default switch) also works but was deliberately not used for the live setup
  checklist — safer for a human pasting commands one at a time — see PR description for the
  exact sequence
- `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN` (Hermes gateway bot), and
  `TELEGRAM_ALLOWED_USERS` live in `~/.hermes/profiles/mercury/.env`; `skills.external_dirs`
  (pointing at `platform/photo-agent/skills`, same path `_demo`'s default profile already
  uses) lives in `~/.hermes/profiles/mercury/config.yaml` — none of this in this repo
- `gateway install` while `mercury` is active installs a separate launchd service
  (`ai.hermes.gateway-mercury.plist`), independent of `_demo`'s default-profile gateway
- Model selection based on task complexity
- Token usage monitoring for cost tracking

### Admin Access
- Single admin user (Phase 1)
- Preferred interface: Telegram (primary), email (fallback)

### Testing Requirements
- All features built using test-driven development
- Unit tests required for all functions and components
- Integration tests required for all end-to-end feature flows
- No feature considered complete without passing tests
- Test suite included in client handoff
- Mercury has no client-specific test files by design — it reuses `platform/photo-agent`'s
  and `platform/email-agent`'s existing test suites, which are already environment-driven and
  client-agnostic (configured via `CLIENT_NAME`/`FIELDKIT_DATA_DIR`/`FIELDKIT_LOG_DIR` env
  vars) rather than hardcoded per client (same pattern `_demo` follows)

### Error Handling Philosophy
- Retry failed operations (email send, API calls)
- Log all errors locally
- Notify admin via Telegram for critical failures
- Graceful degradation when services are unavailable

---

## Decision-Making Framework

When conflicts arise, resolve in this order:

1. **Customer Privacy** — privacy concerns override all other considerations
2. **System Reliability** — system must be stable and always available
3. **Budget Constraints** — stay within cost limits
4. **Human Oversight** — require approval for customer-facing content
5. **Operational Priorities** — follow the phase sequence
6. **Quality Standards** — professional, accurate, helpful
7. **Development Efficiency** — simple, maintainable solutions

---

## Hardware Transfer Plan

**Upon Project Completion:**

1. **System Validation** — all agreed features working, client satisfied
2. **Knowledge Transfer** — training session, documentation handoff, admin guide
3. **Physical Transfer** — Mac Mini delivered to client location, connectivity verified
4. **Post-Transfer Support** — 30-day support period included
5. **Long-Term** — client owns and operates system; source code provided

---

*Established: 2026-08-25*
*Authority: Mercury (demo customer — reference implementation)*
*Framework: FieldKit*
*Deployment: Mac Mini (on-premise, interim)*
*AI Provider: Hermes Agent (Anthropic-backed, dedicated `mercury` profile)*
