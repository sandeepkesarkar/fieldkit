# Platform

> **Intentionally empty.**

This directory is reserved for shared, reusable engines that will be extracted from client implementations once patterns are proven across multiple clients.

## Why is this empty?

Following the FieldKit framework philosophy:

> *"Don't extract reference implementations until patterns proven with N≥2 clients. With only one client, we don't know what's truly reusable."*

All current code lives in individual client directories. Once a second client is onboarded and we can see what's genuinely shared vs client-specific, those patterns will be extracted here.

## What lives here

### Now (spec exists, not yet implemented)

- **Email Agent** — Gmail monitoring, sender allowlist validation, Telegram acknowledgement, local logging
  → Spec: [`.specify/specs/001-email-agent/spec.md`](.specify/specs/001-email-agent/spec.md)

### Future (extracted after N≥2 clients prove the pattern)

- **Social Media** — photo processing, privacy scrubbing, caption generation, multi-platform posting

## Extraction Criteria

Code moves here only when:
- ✅ The pattern has appeared in at least 2 client implementations
- ✅ It solves a recurring problem cleanly
- ✅ It has been validated in production
- ❌ Not client-specific edge cases
- ❌ Not untested or experimental

**Exception:** Infrastructure components that are foundational to all clients — such as the Email Agent and Telegram notifier — may be built directly in `platform/` when their shared nature is established by design, not discovered after the fact. These components are configuration-driven: each client supplies their own credentials and settings, the platform engine is shared.
