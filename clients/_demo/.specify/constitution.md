# Demo Client — Project Constitution

## Introduction

This constitution governs the Demo Client reference implementation. All values marked `[DEMO]` are placeholders — replace with real values when onboarding an actual client.

**Last Updated:** 2026-05-05 (AI Provider section amended 2026-08-23 — see PR #28)
**Status:** Reference / Active
**Deployment Model:** Mac Mini (on-premise, transferred to client after completion)
**AI Provider:** Hermes Agent (self-hosted runtime; Anthropic-backed per current model routing)
**Development Approach:** Phased implementation

---

## Deployment Architecture

### Mac Mini Model

**Hardware Ownership:**
- FieldKit provides Mac Mini for development and deployment
- Mac Mini runs 24/7 at developer location during development
- Upon project completion, Mac Mini transferred to client
- Client owns hardware and can maintain independently

**Implications:**
- AI inference now routes to Anthropic's API via Hermes Agent — no longer self-contained for AI inference specifically
- Non-AI data storage/ownership model is being redefined by the Mac Mini → Cloud pivot (see `platform/.specify/003-hermes-runtime/spec.md`) — "all data stored locally" is not yet re-confirmed post-pivot; replacement model not yet finalized
- Client has full control and ownership
- No recurring hosting costs
- System continues running after FieldKit engagement ends

---

## Core Values

### 1. Customer Privacy & Trust

**Principle:** Customer data and job-site information must never appear in public-facing content without explicit consent.

**Implementation Requirements:**
- [x] All metadata (GPS, timestamps, camera info) stripped from photos before posting
- [x] Human verification required for all customer-facing content
- [x] All customer data stored locally on Mac Mini (not cloud) — pre-Mac-Mini-→-Cloud-pivot state; see note above, not yet re-confirmed post-pivot

---

### 2. Cost Governance & Sustainability

**Principle:** AI spending must be predictable, controlled, and sustainable for a small business.

**Budget Constraints:**
- **Hard Daily Limit:** $5.00 USD per day `[DEMO]`
- **Alert Threshold:** 75% of daily budget consumed
- **Enforcement:** System automatically pauses AI operations when daily limit reached

**Hermes Cost Model:**
- Self-hosted Hermes Agent supervisor process (one-time hardware cost)
- Per-token Anthropic API costs (see Gate 3 / Budget Constraints above)
- External API costs only for other services that can't be self-hosted

**Priority During Budget Constraints:**
1. Email monitoring (must always run)
2. Social media automation

---

### 3. Human Oversight & Quality Control

**Principle:** AI assists human judgment; it does not replace it. All customer-facing content requires human review.

**Approval Requirements:**
- ALL customer-facing content requires admin approval before publication
- Social media posts (captions, images)

**Approval Workflow:**
- Agent sends draft to admin via Telegram
- Admin can approve, request revisions, or reject
- System tracks approval history locally

---

### 4. Operational Priorities

**Phase 1 (Current):** Email intake pipeline
- **Goal:** Admin can send emails to agent and receive Telegram acknowledgements reliably
- **Priority:** Foundation for all other features

**Phase 2 (Next):** Social media automation — photo intake and posting
- **Goal:** Admin emails before/after photos; agent processes and posts with approval

---

### 5. Data Integrity & Preservation

**Principle:** Business data is valuable and should be preserved. Local storage ensures control.

**Data Storage:**
- All data stored on Mac Mini
- No cloud storage except Gmail (email) and Telegram (notifications)
- Data ownership: client owns all data on their Mac Mini

**External Services Used:**
- [x] Gmail (agent email inbox)
- [x] Telegram (admin notifications and commands)

---

## Feature-Specific Principles

### Social Media Management

**Philosophy:** Every post must look professionally crafted and never expose customer privacy.

**Key Principles:**
- Before/after format showcases work quality without identifying customers
- All metadata stripped from photos before any processing
- No post goes live without admin approval

**Platforms:** Instagram, Facebook `[DEMO — confirm with real client]`

---

## Technical Constraints

### Infrastructure Requirements
- Mac Mini M-series
- Reliable internet connection (always-on)
- Dedicated Gmail account for agent email (`agent@[DEMO].com`)
- macOS for deployment

### Hermes Integration
- Self-hosted Hermes Agent runtime on Mac Mini; model calls route to Anthropic's API
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

*Established: 2026-05-05*
*Authority: Demo Client (reference implementation)*
*Framework: FieldKit*
*Deployment: Mac Mini (on-premise)*
*AI Provider: Hermes Agent (Anthropic-backed)*
