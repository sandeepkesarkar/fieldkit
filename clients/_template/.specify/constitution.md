# [Client Name] — Project Constitution

## Introduction

This constitution establishes the governing principles, values, and constraints for [Client Name]'s business automation system. All specifications, technical decisions, and implementation choices must align with these principles.

**Last Updated:** [Date]
**Status:** Draft
**Deployment Model:** [e.g., on-premise hardware, cloud-hosted, hybrid — describe this client's deployment]
**AI Provider:** [Model provider — Hermes Agent runtime, Anthropic (default) or OpenAI (per-client choice); see the FieldKit framework constitution]
**Development Approach:** Phased implementation

---

## Deployment Architecture

### [Deployment Model — e.g., On-Premise Hardware, Cloud-Hosted, Hybrid]

**Hosting & Ownership:**
- [Describe where the system runs and who owns/hosts the infrastructure]
- [Describe any handoff or ownership transfer at project completion, if applicable]

**Implications:**
- AI inference routes to a cloud model provider (Anthropic or OpenAI) via Hermes Agent
- [Describe the data storage/residency model — local, cloud, hybrid]
- [Describe the cost model — recurring hosting costs, one-time hardware cost, etc.]
- [Describe what happens to the system after the FieldKit engagement ends]

---

## Core Values

### 1. Customer Privacy & Trust

**Principle:** [What is your client's privacy commitment to their customers?]

*Prompts to answer:*
- What types of customer information are most sensitive for this industry?
- What must never appear in any customer-facing content? (addresses, faces, license plates, etc.)
- Is location information sensitive? At what level of specificity?
- What consent is required before using customer data or photos?

**Implementation Requirements:**
- [ ] All metadata (GPS, timestamps, camera info) stripped from photos before posting
- [ ] No [industry-specific identifying info] in published content
- [ ] Human verification required for all customer-facing content
- [ ] [Data storage/residency requirement — e.g., local-only, specific cloud region, no third-party storage]
- [ ] [Add client-specific requirements]

---

### 2. Cost Governance & Sustainability

**Principle:** AI spending must be predictable, controlled, and sustainable for a small business.

**Budget Constraints:**
- **Hard Daily Limit:** $[X].00 USD per day
- **Alert Threshold:** [X]% of daily budget consumed (recommended: 75%)
- **Enforcement:** System automatically pauses AI operations when daily limit reached

**Hermes Cost Model:**
- [Agent runtime hosting and associated cost model]
- Per-token API costs to the chosen model provider (Anthropic or OpenAI)
- External API costs for other services that can't be self-hosted (e.g. computer vision, social APIs)

**Priority During Budget Constraints:**
1. [Highest priority feature — e.g. email monitoring]
2. [Second priority]
3. [Third priority]

*Prompt: What is the most critical function that must keep working even if budget is tight?*

---

### 3. Human Oversight & Quality Control

**Principle:** AI assists human judgment; it does not replace it. All customer-facing content requires human review.

**Approval Requirements:**
- ALL customer-facing content requires admin approval before publication
- [List specific content types: social media posts, etc.]

**Approval Workflow:**
- [Describe how admin receives drafts and approves/rejects — email, WhatsApp, etc.]
- Admin can approve, request revisions, or reject
- System tracks approval history

*Prompt: What is the admin's preferred channel for receiving and approving content?*

---

### 4. Operational Priorities

**Principle:** Not all business functions have equal urgency. Phased implementation reflects operational realities.

**Phase 1 (Current):** [Describe what Phase 1 covers]
- **Goal:** [What does success look like for Phase 1?]
- **Priority:** [Why this phase first?]

**Phase 2 (Next):** [Describe Phase 2]
- **Goal:** [What does success look like?]
- **Priority:** [Why this order?]

**Phase 3 (Future):** [Describe Phase 3 if applicable]

*Prompt: What is the single highest-value thing this system could do for the business right now?*

---

### 5. Data Integrity & Preservation

**Principle:** Business data is valuable and should be preserved. [Describe this client's approach to data control.]

**Data Storage:**
- [Data storage/residency model — e.g., local-only, cloud, hybrid]
- [Cloud storage policy, if any — list any necessary external services]
- Backup strategy: [Who is responsible? What is the approach?]
- Data ownership: [Client name] owns all data — [describe where/how it's stored]

**External Services Used:**
- [ ] Gmail (email)
- [ ] Google Drive (file storage)
- [ ] [Other services]

*Prompt: What data is most critical to preserve? What would be catastrophic to lose?*

---

## Feature-Specific Principles

### [Feature Name — e.g. Social Media Management]

**Philosophy:** [One sentence on the governing idea for this feature]

**Key Principles:**
- [Principle 1]
- [Principle 2]
- [Principle 3]

**Platforms:** [Which platforms are in scope?]

---

## Technical Constraints

### Infrastructure Requirements
- [Deployment hardware/platform — e.g., on-premise Mac Mini, cloud VM, managed service]
- Reliable internet connection (always-on)
- Gmail account for email workflows
- [Operating system / platform requirement, if any]

### Hermes Integration
- Hermes Agent runtime; model calls route to the chosen cloud provider (Anthropic or OpenAI)
- Model selection based on task complexity
- Token usage monitoring for cost tracking

### Admin Access
- Single admin user (Phase 1)
- [Preferred interface: email / WhatsApp / web dashboard]

### Testing Requirements
- All features built using test-driven development
- Unit tests required for all functions and components
- Integration tests required for all end-to-end feature flows
- No feature considered complete without passing tests
- Test suite included in client handoff

### Error Handling Philosophy
- Retry failed operations (email send, API calls)
- Log all errors locally
- Email admin for critical failures
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

*Prompt: Are there any client-specific priorities that would override this order?*

---

## Transition Plan

**Upon Project Completion:**

1. **System Validation** — all agreed features working, client satisfied
2. **Knowledge Transfer** — training session, documentation handoff, admin guide
3. **Handoff** — [describe what's transferred — e.g., physical hardware, cloud account access, credentials — based on this client's deployment model]
4. **Post-Transfer Support** — 30-day support period included
5. **Long-Term** — client owns and operates system; source code provided

---

## Acknowledgment

By implementing this system, all stakeholders agree to:
- Respect the privacy principles above
- [Maintain deployment infrastructure properly, per the deployment model above]
- Operate within budget constraints
- Follow approval workflows
- Prioritize customer satisfaction and business sustainability

---

*Established: [Date]*
*Authority: [Client Name] Business Owner*
*Framework: FieldKit*
*Deployment: [on-premise / cloud-hosted / hybrid — per client]*
*AI Provider: [Hermes Agent — Anthropic or OpenAI per client]*
