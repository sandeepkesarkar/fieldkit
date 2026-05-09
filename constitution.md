# [Client Name] — Project Constitution

## Introduction

This constitution establishes the governing principles, values, and constraints for [Client Name]'s business automation system. All specifications, technical decisions, and implementation choices must align with these principles.

**Last Updated:** [Date]
**Status:** Draft
**Deployment Model:** Mac Mini (on-premise, transferred to client after completion)
**AI Provider:** OpenClaw (self-hosted, open-source)
**Development Approach:** Phased implementation

---

## Deployment Architecture

### Mac Mini Model

**Hardware Ownership:**
- FieldKit provides Mac Mini for development and deployment
- Mac Mini runs 24/7 at developer location during development
- Upon project completion, Mac Mini transferred to [Client Name]
- [Client Name] owns hardware and can maintain independently

**Implications:**
- Self-contained system — no cloud dependencies for AI inference
- All data stored locally on Mac Mini
- Client has full control and ownership
- No recurring hosting costs
- System continues running after FieldKit engagement ends

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
- [ ] All customer data stored locally on Mac Mini (not cloud)
- [ ] [Add client-specific requirements]

---

### 2. Cost Governance & Sustainability

**Principle:** AI spending must be predictable, controlled, and sustainable for a small business.

**Budget Constraints:**
- **Hard Daily Limit:** $[X].00 USD per day
- **Alert Threshold:** [X]% of daily budget consumed (recommended: 75%)
- **Enforcement:** System automatically pauses AI operations when daily limit reached

**OpenClaw Cost Model:**
- Self-hosted on Mac Mini (one-time hardware cost, no per-API-call fees)
- Electricity costs (minimal for Mac Mini)
- External API costs only for services that can't be self-hosted (e.g. computer vision, social APIs)

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

**Principle:** Business data is valuable and should be preserved. Local storage ensures control.

**Data Storage:**
- All data stored on Mac Mini
- No cloud storage (except necessary external services — list them)
- Backup strategy: [Who is responsible? What is the approach?]
- Data ownership: [Client name] owns all data on their Mac Mini

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
- Mac Mini M-series
- Reliable internet connection (always-on)
- Gmail account for email workflows
- macOS for deployment

### OpenClaw Integration
- Self-hosted LLM deployment on Mac Mini
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

## Hardware Transfer Plan

**Upon Project Completion:**

1. **System Validation** — all agreed features working, client satisfied
2. **Knowledge Transfer** — training session, documentation handoff, admin guide
3. **Physical Transfer** — Mac Mini delivered to client location, connectivity verified
4. **Post-Transfer Support** — 30-day support period included
5. **Long-Term** — client owns and operates system; source code provided

---

## Acknowledgment

By implementing this system, all stakeholders agree to:
- Respect the privacy principles above
- Maintain Mac Mini hardware properly
- Operate within budget constraints
- Follow approval workflows
- Prioritize customer satisfaction and business sustainability

---

*Established: [Date]*
*Authority: [Client Name] Business Owner*
*Framework: FieldKit*
*Deployment: Mac Mini (on-premise)*
*AI Provider: OpenClaw (self-hosted)*
