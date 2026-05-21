# FieldKit Framework Constitution

**Scope:** Framework-level principles that apply to ALL client work.
**Authority:** Every feature spec, plan, and implementation must satisfy every gate below.
**Client-specific additions:** See `{client}/.specify/constitution.md`.

---

## Gates (must pass before any implementation begins)

### Gate 1 — Privacy

- ALL metadata (GPS, timestamps, camera info, faces) stripped from media before any output
- No customer-identifying information (addresses, license plates, names) in published content
- Customer data stored only on the client's Mac Mini — never uploaded to external cloud storage
- Consent must be established before using any customer data or photos

### Gate 2 — Human-in-the-Loop

- ALL customer-facing content requires explicit admin approval before publication
- AI generates drafts; humans make final decisions
- Admin must be able to approve, revise, or reject at every gate
- System must never publish or act irrevocably without documented human sign-off

### Gate 3 — Budget Governance

- No unbounded AI API calls; every feature must have a hard daily cost limit
- AI operations must pause automatically when daily budget is exhausted
- Priority queue: most critical features keep running under budget pressure
- Cost tracking logged locally on every run

### Gate 4 — Client Ownership

- All code and data owned by the client — no proprietary lock-in
- Mac Mini hardware transferred to client upon project completion
- Full test suite included in client handoff
- System continues operating after FieldKit engagement ends

### Gate 5 — Test-Driven Development

- Tests must be written before or alongside implementation — never after
- Unit tests required for every function and component
- Integration tests required for every end-to-end feature flow
- A feature is NOT complete until all tests pass

---

## Decision Priority Order

When conflicts arise, resolve in this order:

1. Customer Privacy (overrides everything)
2. System Reliability (must be stable and always available)
3. Budget Constraints (stay within cost limits)
4. Human Oversight (approval required for customer-facing content)
5. Operational Priorities (follow the phased implementation plan)
6. Development Efficiency (simple, maintainable solutions)

---

## Architecture Constraints

- **Runtime:** OpenClaw (self-hosted on Mac Mini)
- **Platform:** macOS (Mac Mini M-series), Python 3.11+
- **Testing:** pytest + pytest-mock
- **No cloud AI inference** — all LLM work runs locally via OpenClaw
- **No cloud storage** — Google Drive is the exception for client-initiated uploads only
- **Admin interface:** Telegram (commands + callback keyboards)

---

## Spec-Kit Workflow (FieldKit additions)

Every feature must produce all of the following before implementation starts:

| Artifact | Command | Description |
|----------|---------|-------------|
| `spec.md` | `/speckit-specify` | Requirements — what + why, no tech |
| `clarify.md` | `/speckit-clarify` | Q&A resolving ambiguities |
| `plan.md` | `/speckit-plan` | Technical plan with stack decisions |
| `sequence-diagram.md` | `/speckit-plan` | Mermaid sequenceDiagram of happy path |
| `tasks.md` | `/speckit-tasks` | Numbered implementation tasks |
| `features/*.feature` | `/speckit-tasks` | Gherkin acceptance tests |

---

**Version:** 1.0 | **Ratified:** 2026-05-20 | **Framework:** FieldKit
