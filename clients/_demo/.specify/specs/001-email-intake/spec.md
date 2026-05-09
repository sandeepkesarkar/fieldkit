# 001 — Email Intake

**Status:** Spec
**Platform Engine:** [`platform/.specify/specs/001-email-agent/spec.md`](../../../../../platform/.specify/specs/001-email-agent/spec.md)
**Last Updated:** 2026-05-05

---

## Purpose

Enable the admin to send emails to the agent and receive a Telegram acknowledgement confirming receipt. This is the communication foundation — all subsequent features (social media automation, etc.) use this same email channel as their input.

---

## This Client Uses the Platform Email Agent

The full behavior specification lives in the platform-level spec linked above. This document captures the Demo Client's specific configuration and any client-level decisions on top of the platform defaults.

---

## Demo Client Configuration

| Parameter | Demo Value |
|-----------|------------|
| `agent_email` | `fieldkit.demo.agent@gmail.com` `[DEMO — placeholder]` |
| `admin_allowlist` | `admin@demo-business.com` `[DEMO — placeholder]` |
| `telegram_bot_token` | `[DEMO — set in environment, never committed]` |
| `admin_telegram_chat_id` | `[DEMO — set in environment, never committed]` |
| `polling_interval_minutes` | `5` |

---

## Client-Level Decisions

- **Telegram is the primary acknowledgement channel.** The admin is active on Telegram and prefers it over email for agent communication.
- **Email fallback is acceptable.** If Telegram is down, an email reply to the original message is sufficient.
- **Silent rejection to unknown senders, but admin is notified via Telegram.** No auto-reply to the unknown sender (avoids revealing the agent address to spammers), but the admin always receives a Telegram notification when a rejection occurs so they can identify misconfigured senders or their own mistakes.
- **`/check-email` is the on-demand trigger command.** Admin can issue `/check-email` in Telegram to force an immediate inbox read.

---

## Out of Scope (this feature)

- Reading or parsing email body content
- Processing attachments
- Any AI processing
- Queuing emails for downstream social media workflow

*(These are addressed in subsequent feature specs.)*
