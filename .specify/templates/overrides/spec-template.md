# [NNN] — [Feature Name]

**Status:** Spec
**Type:** [Platform feature | Client feature (N≥2 rule — not yet extracted to platform)]
**Last Updated:** [DATE]

---

## Purpose

[One paragraph: what this feature does and why it exists in the context of the client's business.]

---

## Scope

**In scope:**
- [Capability 1]
- [Capability 2]

**Out of scope:**
- [What this feature explicitly does not do]

---

## User Stories *(mandatory)*

<!--
  Each story must be independently testable and deliver standalone value.
  Assign priorities P1, P2, P3... where P1 is the most critical.
  Written for business stakeholders — no implementation details.
-->

### Story 1 — [Brief Title] (P1)

[Describe the user journey in plain language.]

**Why this priority:** [Business value and urgency]

**Independent test:** [How this can be validated in isolation]

**Acceptance scenarios:**

1. **Given** [initial state], **When** [action], **Then** [expected outcome]
2. **Given** [initial state], **When** [action], **Then** [expected outcome]

---

### Story 2 — [Brief Title] (P2)

[Describe the user journey.]

**Why this priority:** [Business value]

**Acceptance scenarios:**

1. **Given** [initial state], **When** [action], **Then** [expected outcome]

---

[Add more stories as needed]

### Edge Cases

- What happens when [boundary condition]?
- How does the system handle [error scenario]?

---

## Functional Requirements *(mandatory)*

- **FR-001:** System MUST [specific, testable capability]
- **FR-002:** System MUST [specific, testable capability]
- **FR-003:** Admin MUST be able to [key interaction]
- **FR-004:** System MUST [data or state requirement]
- **FR-005:** System MUST [reliability or recovery requirement]

---

## Privacy & Human-in-the-Loop *(mandatory for all FieldKit features)*

**Privacy requirements:**
- [What customer data or identifying information must never appear in any output]
- [Metadata stripping requirements — GPS, timestamps, faces, addresses, etc.]
- [Consent or approval requirements before using customer data]

**Human approval requirements:**
- ALL customer-facing content requires admin approval before publication
- [Specific approval workflow for this feature]
- Admin can approve, request revisions, or reject at each gate
- System must never publish or act irrevocably without explicit admin sign-off

---

## Success Criteria *(mandatory)*

- **SC-001:** [Measurable user-facing metric — e.g., "Admin completes approval in under 2 minutes"]
- **SC-002:** [Reliability metric — e.g., "System processes photos without data loss 100% of the time"]
- **SC-003:** [Privacy metric — e.g., "Zero customer-identifying information in any published content"]
- **SC-004:** [Business metric — e.g., "Reduces manual posting time by 80%"]

---

## Constraints & Assumptions

**Constraints:**
- Runs on a Mac Mini (on-premise, transferred to client after completion)
- Must operate within daily AI budget limit
- No cloud storage — all data stays on Mac Mini
- [Feature-specific constraints]

**Assumptions:**
- [Assumption about admin workflow or environment]
- [Assumption about external service availability]
- [Assumption about scope boundaries]
