# 003 — Hermes Runtime Migration

**Status:** Spec
**Type:** Platform feature
**Last Updated:** 2026-08-21 (FR-002a amendment, issue #8)

---

## Purpose

Replace OpenClaw with [Hermes Agent](https://github.com/NousResearch/hermes-agent) as FieldKit's chat-gateway and skill-dispatch runtime. This is a hard swap: Hermes becomes required, not optional, the way OpenClaw was. No OpenClaw state is carried over — Hermes is configured from scratch. Scope is FieldKit only; servicehub is being retired and is untouched by this work.

---

## Scope

**In scope:**
- Replacing OpenClaw's Telegram gateway with Hermes's gateway
- Rewriting the two chat-driven skill dispatches (`process_photos` on-demand command, `check_approval` on-demand command) as Hermes skills — see FR-002a: `check_approval`'s button-callback trigger is not a Hermes skill dispatch and stays on its existing cron leg
- Model provider configuration: Anthropic by default, OpenAI as an explicit per-client choice
- Preserving governance behavior that actually exists in code today (approval gate, admin allowlist)
- Updating `constitution.md` and the spec-kit override template to remove architecture claims retired by the Mac Mini → Cloud pivot
- Creating two demo customers (one Anthropic-backed, one OpenAI-backed) as first-class FieldKit clients under Option C

**Out of scope:**
- Any change to the cron-triggered scripts (`check_email.py`, `upload_facebook.py`, and `check_approval.py`'s cron leg) — these already call `python3` directly with zero OpenClaw dependency and stay exactly as they are
- Migrating scheduling into Hermes's built-in cron scheduler — deferred, not ruled out
- Adopting Hermes's native conversational email gateway — email-agent stays a deterministic, no-LLM gate-and-acknowledge script
- Closing the EXIF/GPS-stripping and daily-$-cost-cap governance gaps — pre-existing and runtime-independent (see Known Gaps)
- servicehub — being retired, untouched
- Multi-model routing beyond the Anthropic/OpenAI per-client choice
- `hermes claw migrate` or any other OpenClaw state carryover

---

## User Stories

### Story 1 — Admin drives the pipeline over Telegram, same as today (P1)

As the admin, I send Telegram commands and tap approve/reject buttons exactly as I do today, and the agent dispatches the right skill — except the gateway underneath is now Hermes, not OpenClaw.

**Why this priority:** This is the entire user-facing surface of the runtime. If it doesn't work, nothing else matters.

**Independent test:** Send `/check_approval` (manual command, dispatched by Hermes) and separately tap Approve on a pending video (handled by `check_approval.py`'s existing cron leg, not Hermes — see FR-002a); confirm the same photo-pipeline behavior as under OpenClaw in both cases.

**Acceptance scenarios:**

1. **Given** a pending photo approval, **When** the admin taps Approve in Telegram, **Then** `check_approval.py`'s existing cron leg (unchanged, FR-003) detects and processes the button tap — **not** Hermes; see FR-002 and FR-002a for why Hermes cannot dispatch on the raw button-callback trigger — and the video is queued for the existing cron-driven upload.
2. **Given** the admin sends the manual command to process photos or check approval status, **When** Hermes receives it, **Then** it dispatches `process_photos.py` or `check_approval.py` respectively and relays output verbatim — no improvising, no summarizing — matching the current `SKILL_*.md` instructions.

---

### Story 2 — Two demo customers run in parallel on different providers (P2)

As FieldKit's operator, I have two demo customers — one on Anthropic, one on OpenAI — under Option C (each a complete spec-kit client instance), so every future capability gets tested against both providers before it ships to a real client.

**Why this priority:** Validates the "OpenAI supported as a per-client configuration choice" decision with a real, working example, not just a spec claim.

**Acceptance scenarios:**

1. **Given** the Anthropic-backed demo customer, **When** any chat-driven skill runs, **Then** the underlying model calls go to Anthropic.
2. **Given** the OpenAI-backed demo customer, **When** the same skill runs, **Then** the underlying model calls go to OpenAI, with identical skill behavior.

---

### Story 3 — Governance behavior that exists today keeps working (P1)

As the admin, the approval gate and the admin allowlist enforcement keep working exactly as they do under OpenClaw — Hermes doesn't weaken anything that's actually implemented today.

**Why this priority:** Gate 2 (Human-in-the-Loop) is the constitution's second gate. Regressing it is not acceptable, even during a runtime swap.

**Acceptance scenarios:**

1. **Given** customer-facing content pending publication, **When** it reaches the approval gate, **Then** it is not published until the admin explicitly approves it, regardless of runtime.
2. **Given** an email from a non-allowlisted sender, **When** it arrives, **Then** it is rejected and logged, unaffected by the runtime swap — `check_email.py` is untouched by this feature.

### Edge Cases

- What happens if Hermes's gateway process crashes mid-approval? Today OpenClaw runs under `launchd`; Hermes needs an equivalent always-on supervisor (`hermes gateway install`).
- What happens if a chat-driven skill is invoked while the cron leg for the same resource (e.g. `check_approval`) is mid-run? Existing race-condition handling in `check_approval.py` is caller-agnostic and should be unaffected.
- What happens when `check_approval.py`'s cron leg and Hermes's gateway both call Telegram `getUpdates` on the shared bot token? Per FR-002a, this is a known, unresolved risk of the same `409 Conflict` #6 hit between OpenClaw and Hermes — tracked as a follow-up, not fixed by this feature.
- What happens if OpenAI is selected for a demo customer but the API key is missing or invalid? Must fail loud at startup or first call, not silently.

---

## Functional Requirements

- **FR-001:** Hermes MUST replace OpenClaw as the Telegram gateway process on the Mac Mini, running under an always-on supervisor equivalent to today's `launchd` daemon.
- **FR-002:** Hermes MUST dispatch `process_photos.py` on the existing manual-command trigger and `check_approval.py` on the existing manual `/check_approval` command trigger, relaying output verbatim, matching current `SKILL_*.md` instructions.
- **FR-002a:** The Approve/Reject button-callback trigger MUST continue to be handled exclusively by `check_approval.py`'s pre-existing cron leg (FR-003), independent of Hermes. **Amended 2026-08-21 (issue #8):** the original FR-002 draft assumed Hermes could dispatch a skill directly off the raw button tap, by analogy with OpenClaw. Verified empirically against the local Hermes install that this is not possible: `plugins/platforms/telegram/adapter.py::_handle_callback_query` only recognizes a closed set of Hermes-internal `callback_data` prefixes (`mp:`, `cp:`, `gt:`, `ea:`, `sc:`, `cl:`, `update_prompt:`); a `callback_data` of `"approve"` or `"reject"` (FieldKit's own payload, from `tools/telegram_api.py::send_message_with_buttons`) falls through every branch and the handler returns having done nothing — no `answer_callback_query`, no `edit_message_text`, and critically no skill or agent-turn dispatch of any kind (confirmed by directly invoking `_handle_callback_query` with both values under Hermes's own venv). `_normalize_platform_event`, Hermes's only other generic inbound-event hook, is wired for `message_reaction` and `edited_message` only and returns `None` for a `callback_query` update, so there is no alternate escape hatch either. Hermes's own `docs/relay-connector-contract.md` documents the same posture by design: *"Foreign callback payloads (another integration's buttons) never become prompt events... dropped at the connector."* This is a structural limitation of Hermes's current callback-handling design, not a configuration or naming gap (contrast with the `process-photos` naming question in #7/#18, which *was* resolved by configuration). No workaround was pursued within fieldkit's scope, since it would require patching Hermes itself (an installed dependency, not a fieldkit-owned file). **Known follow-up risk, not resolved by this amendment:** Hermes's gateway now holds the sole active `getUpdates` long-poll on the shared bot token (see `platform/docs/hermes/02-gateway-setup.md`'s "single-poller conflict" note from #6); `check_approval.py`'s cron leg polls the same token independently, which risks the identical `409 Conflict` #6 hit between OpenClaw and Hermes. Whether this conflict actually manifests in practice, and if so how to resolve it, is out of scope for #8 and tracked as a separate follow-up.
- **FR-003:** The cron-triggered scripts (`check_email.py`, `check_approval.py`'s cron leg, `upload_facebook.py`) MUST remain unchanged — no Hermes dependency is introduced into their invocation path. Per FR-002a, `check_approval.py`'s cron leg is now the sole mechanism handling the Approve/Reject button-callback trigger.
- **FR-004:** Model routing MUST default to Anthropic and MUST support OpenAI as an explicit per-client configuration choice, with no other behavioral difference between providers.
- **FR-005:** Two demo customers MUST exist as first-class FieldKit clients under the existing Option C monorepo model (`clients/{name}/.specify/...`) — one Anthropic-backed, one OpenAI-backed.
- **FR-006:** `constitution.md`'s Architecture Constraints MUST be updated to reflect Hermes (not OpenClaw) as the runtime, and MUST no longer state architecture facts retired by the Mac Mini → Cloud pivot (see Constitution Updates below).
- **FR-007:** The FieldKit spec-kit override template (`.specify/templates/overrides/spec-template.md`) MUST have its boilerplate Constraints section updated to match — it currently states "Runs on a Mac Mini (on-premise, transferred to client after completion)" and "No cloud storage — all data stays on Mac Mini," both retired.
- **FR-008:** No OpenClaw state, config, or `hermes claw migrate` output MUST be carried into the new setup — Hermes is configured from scratch.
- **FR-009:** This feature MUST be executed through the Foundation GitHub task workflow (`dev-infrastructure/specs/github-task-workflow.md`) — dogfooding the workflow on real FieldKit work.

---

## Privacy & Human-in-the-Loop

**Privacy requirements:**
- No change to what's already enforced in code: PII redaction in logging (`facebook_logger.py`), admin-only allowlist for email intake.
- **Known gap, not addressed by this feature:** EXIF/GPS/camera-metadata stripping is not currently implemented in the photo pipeline despite constitution Gate 1 requiring it. This spec does not close that gap — see Known Gaps.
- Consent and photo/video handling behavior is unchanged; this feature swaps the runtime only, not the pipeline logic.

**Human approval requirements:**
- ALL customer-facing content still requires explicit admin approval before publication — unchanged, verified in Story 3.
- The Telegram approve/reject UX is unchanged from the admin's perspective; only the process underneath differs.
- System must never publish or act irrevocably without documented human sign-off — unchanged.

---

## Success Criteria

- **SC-001:** Every existing chat-driven and cron-driven flow (`check_email`, `process_photos`, `check_approval`, `upload_facebook`) works under Hermes with no admin-visible behavior change.
- **SC-002:** Both demo customers (Anthropic-backed, OpenAI-backed) complete an end-to-end photo-approval cycle successfully.
- **SC-003:** OpenClaw is fully uninstalled from the Mac Mini with no remaining dependency in FieldKit.
- **SC-004:** `constitution.md` and the spec-kit override template no longer contain architecture claims retired by the cloud pivot.
- **SC-005:** This feature is decomposed into issues and executed through the Foundation GitHub task workflow.

---

## Constraints & Assumptions

**Constraints:**
- Runs on the Mac Mini for now (interim development home per plan-of-record) — NOT transferred to the client, and NOT the permanent home; W3 defines the actual per-client cloud deployment.
- Must operate within a daily AI budget limit in principle (constitution Gate 3) — enforcement mechanism is a known, pre-existing gap, not solved by this feature.
- No new cloud storage introduced by this feature; Google Drive remains the existing client-initiated-upload exception.
- FieldKit only — no servicehub changes.

**Assumptions:**
- Hermes's Telegram gateway is a drop-in replacement for OpenClaw's from the admin's perspective (same bot, same chat, same UX).
- `gh` CLI, Python 3.11+, and the existing photo-agent/email-agent tooling remain unaffected — only the gateway/dispatch layer changes.
- The two demo customers are created fresh (not adapted from `_demo`), independent artifacts under Option C.
- Omnigent (W2, dev-infrastructure) can also run Hermes as a harness — noted for later; does not affect this spec's decisions.

---

## Known Gaps (carried forward, not resolved by this feature)

- **EXIF/GPS/camera-metadata stripping** (constitution Gate 1) has no implementation in the current photo pipeline, under either OpenClaw or Hermes. Flagged for separate follow-up; not blocking this runtime swap.
- **Daily $ cost cap** (constitution Gate 3) has no implementation anywhere in the repo, under either OpenClaw or Hermes. Flagged for separate follow-up.

---

## Constitution Updates (FR-006, FR-007)

Proposed changes to `.specify/memory/constitution.md` → Architecture Constraints:
- `Runtime: OpenClaw (self-hosted on Mac Mini)` → `Runtime: Hermes Agent (self-hosted on Mac Mini; see dev-infrastructure plan-of-record for the cloud roadmap)`
- Remove or rewrite `No cloud AI inference — all LLM work runs locally via OpenClaw` — already inaccurate (OpenClaw calls the OpenAI API today) and superseded by explicit cloud model routing (Anthropic/OpenAI).
- Remove or rewrite `Mac Mini hardware transferred to client upon project completion` (Gate 4) and `Customer data stored only on the client's Mac Mini — never uploaded to external cloud storage` (Gate 1) — both retired by the Mac Mini → Cloud pivot; W3 defines the replacement model.

Proposed change to `.specify/templates/overrides/spec-template.md` → Constraints boilerplate:
- Remove `Runs on a Mac Mini (on-premise, transferred to client after completion)` and `No cloud storage — all data stays on Mac Mini` from the default template text; replace with placeholders that don't presume the retired architecture.
