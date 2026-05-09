# 001 — Email Agent: Clarifications

Resolved ambiguities from `spec.md`. These decisions are binding for technical planning.

---

## Configuration Storage

All parameters from the Configuration Interface are stored in a single `.env` file on the Mac Mini.

| Parameter | Stored in |
|-----------|-----------|
| `agent_email` | `.env` |
| `admin_allowlist` | `.env` |
| `telegram_bot_token` | `.env` |
| `admin_telegram_chat_id` | `.env` |
| `polling_interval_minutes` | `.env` |

The `.env` file is never committed to the client repo. It lives only on the Mac Mini.

**Why:** Fewer files, simpler mental model for a one-person setup. No distinction between secrets and non-secrets — everything is treated as sensitive.

---

## Ref ID Persistence

The ref ID counter is stored in `~/fieldkit/data/email-agent/state.json` on the Mac Mini. The agent reads this file on startup and writes to it after each new email is assigned a ref ID. This path is outside the code directory — it is never committed.

**Why:** No dependencies, no setup. A counter doesn't need a database.

---

## Double-Processing Guard

**Primary deduplication: `state.json` processed map.** Before assigning a ref ID, `state.py` checks whether the Gmail message ID already exists in the `processed` map. If so, it reuses the existing ref ID — the admin receives a duplicate acknowledgement with the same ref ID, making the duplicate obvious.

**Secondary deduplication: Gmail query filter.** The list query uses `is:unread -label:fk-received` to skip most already-processed emails efficiently. This is a performance optimisation, not the source of truth — if the label was not applied (due to a `gws` error), the processed map catches it.

If the agent crashes between processing an email and applying the label, the email will be picked up again on the next cycle. `state.py` reuses the ref ID. The admin receives a duplicate ack with the same ref ID — acceptable, and clearly identifiable as a duplicate.

`state.json` stores a `processed` map of Gmail message ID → ref ID for this purpose:

```json
{
  "last_ref_id": 14,
  "processed": {
    "18f3a2b1c4d5e6f7": "#0014"
  },
  "fk_received_label_id": "Label_12345678"
}
```

**Why:** Without this, a crash mid-cycle produces two acks with different ref IDs for the same email — the admin has no way to tell they're duplicates and may go looking for a second job that doesn't exist. Reusing the same ref ID makes the duplication obvious and harmless.

---

## File Locking

All read-modify-write operations on `state.json` and `pending.json` must acquire an exclusive file lock before reading and release it after writing. This prevents data corruption when cron and `/check-email` run concurrently.

Implementation: use `fcntl.flock(fd, fcntl.LOCK_EX)` before read, `fcntl.LOCK_UN` after write. macOS (the Mac Mini platform) supports this natively.

**Why:** Two concurrent agent runs — one cron, one `/check-email` — both read `state.json`, both see `last_ref_id: 14`, both increment to 15, and both write back. One write clobbers the other. The result is two emails with the same ref ID — which defeats the entire tracking purpose.

---

## When Both Telegram and Email Fallback Fail

The agent logs the failure and takes no further action. No retry queue, no re-attempt on the next cycle.

**Why:** Both delivery channels being down simultaneously is a rare edge case. Retry logic adds complexity that isn't justified at this stage. The admin will notice if acknowledgements stop arriving.

---

## ADMIN_ALLOWLIST Parsing Rules

The `ADMIN_ALLOWLIST` env var is a comma-separated string. The following rules apply at matching time:

- **Whitespace:** trim leading/trailing whitespace around each entry after splitting on `,`
- **Case:** lowercase both the allowlist entry and the incoming sender before comparing
- **Header:** use the `From:` header only — not `Reply-To:` or `Return-Path:`
- **Display name:** if the `From:` value is in `"Display Name" <email@example.com>` format, extract only the address part (`email@example.com`) before comparing
- **Malformed header:** if the `From:` header cannot be parsed to a valid email address, treat the email as an unknown sender and reject it

**Why:** These are the most common real-world failure modes: a sender whose name appears in the From header, or an allowlist entry with accidental whitespace. Defining the rules here keeps the SKILL.md instructions unambiguous.

---

## Undelivered Notification Handling (Dead-Letter Queue)

Real-time Telegram delivery confirmation is not required. OpenClaw may fire-and-forget Telegram messages — attempting to observe delivery status adds complexity that is not justified for a rare failure mode. Instead, a dead-letter queue pattern is used:

**pending.json** — a local queue file at `~/fieldkit/data/email-agent/pending.json`. Structure:

```json
{
  "pending": [
    {
      "ref_id": "#0014",
      "gmail_message_id": "18f3a2b1c4d5e6f7",
      "from": "admin@example.com",
      "subject": "Job #42",
      "queued_at": "2026-05-09T14:32:00Z"
    }
  ]
}
```

**Flow:**
1. Before sending each Telegram ack, write the entry to `pending.json`
2. Send the Telegram message (no delivery confirmation needed)
3. Remove the entry from `pending.json` immediately after the send attempt, regardless of outcome
4. On each cycle, check `pending.json` for entries older than **15 minutes**
5. If stale entries found: send one alert email via `gws gmail +send` to the first address in `ADMIN_ALLOWLIST`, listing the ref IDs; then clear those entries

**Alert email format:**
```
Subject: ⚠️ FieldKit: Possible undelivered notifications
Body:
These acknowledgements may not have been delivered via Telegram:

Ref #0014 — Job #42 (queued 2026-05-09 14:32)

Check Telegram history or send /check-email to confirm.
```

**Why DLQ over retry:** Retry chains require delivery status observability (which OpenClaw may not provide), add timing complexity (30s wait), and introduce a second delivery channel to maintain. A stale alert after 15 minutes is a simpler, human-in-the-loop approach that matches the FieldKit constitution's preference for human oversight over automated recovery.

---

## Email Trigger Model

Phase 1 uses an **OpenClaw cron job** polling every `polling_interval_minutes` (default 5). This is simpler and easier to debug than an event-driven model.

`gws gmail +watch` (streams new emails as NDJSON via Google PubSub) is the target upgrade path — it would make delivery instant rather than up-to-5-minutes delayed. This is deferred to a later phase.

**Why cron first:** Predictable, debuggable, no webhook infrastructure needed. The 5-minute delay is acceptable for Phase 1. `+watch` can be layered on without changing the processing logic — only the trigger mechanism changes.

**To implement `+watch` later:**
- Run `gws gmail +watch` as a persistent process
- Pipe NDJSON output into OpenClaw's webhook endpoint (`POST /hooks/wake`)
- Remove or keep the cron job as a fallback

---

## `/check-email` Response Volume

The agent always sends one Telegram message per new valid email, regardless of how many are waiting. No summary threshold, no batching.

**Why:** Keeps `/check-email` behavior identical to the polling cycle behavior. No special cases to reason about.
