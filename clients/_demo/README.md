# Demo Client — FieldKit Reference Implementation

**Client:** Demo Client (reference only — not a real engagement)
**Industry:** General / Reference
**Location:** N/A
**Status:** In Progress — Spec phase
**Deployment:** Mac Mini (on-premise)
**AI Provider:** Hermes (self-hosted)

---

## Overview

This is the `_demo` client — a reference implementation used for development, testing, and illustrating how FieldKit works end-to-end. It uses clearly fake credentials and placeholder data. It is not a real client engagement.

Real client implementations live in separate private repositories. See [`clients/README.md`](../README.md) for the architecture decision behind this.

---

## Feature Status

| Feature | Spec | Clarify | Plan | Build | Live |
|---------|------|---------|------|-------|------|
| 001 — Email Intake | ✅ | ⏳ | ⏳ | ⏳ | ⏳ |

---

## Governance

All decisions are governed by the project constitution:
→ [`.specify/memory/constitution.md`](.specify/memory/constitution.md)

Platform-level engine specs:
→ [`platform/.specify/specs/001-email-agent/spec.md`](../../platform/.specify/specs/001-email-agent/spec.md)

---

## Development Workflow

| Step | Status |
|------|--------|
| Constitution | ✅ |
| Specification | ✅ (001 — Email Intake) |
| Clarification | ⏳ |
| Technical Planning | ⏳ |
| Task Breakdown | ⏳ |
| Implementation | ⏳ |
| Production | ⏳ |

---

## Next Steps

1. [x] Fill in constitution
2. [x] Write first feature spec (001 — Email Intake)
3. [ ] Run `/speckit.clarify` on email intake spec
4. [ ] Decide technology stack (Python? Node?)
5. [ ] Create technical plan with `/speckit.plan`
