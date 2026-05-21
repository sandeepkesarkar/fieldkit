# 001 — Email Agent

**Status:** Spec
**Type:** Platform (shared infrastructure)
**Last Updated:** 2026-05-05

---

## Purpose

The Email Agent is the foundational input channel for all FieldKit automation. It monitors a dedicated Gmail inbox on behalf of a client's agent, validates incoming emails, and confirms receipt to the admin via Telegram.

Every higher-level feature — social media automation and anything that follows — uses this channel as its input mechanism. Admin instructions and content arrive by email; this component ensures the agent always receives them reliably and the admin always knows they were received.

---

## Scope

**In scope:**
- Monitoring a dedicated Gmail inbox for new unread emails
- Validating sender against a configurable allowlist
- Assigning a reference ID and logging every received/rejected email locally
- Sending a Telegram acknowledgement to the admin for every valid email
- Alerting admin via email if Telegram notifications appear undelivered for more than 15 minutes
- Providing an on-demand inbox check trigger via a Telegram command

**Out of scope:**
- Reading, parsing, or acting on email body content
- Processing or storing email attachments
- Any AI or LLM processing
- Queuing emails for downstream feature processing
- Multi-admin support
- Email threading or conversation tracking

---

## Actors

- **Admin** — the business owner or designated operator; sends emails to the agent address, receives Telegram acknowledgements, can issue Telegram commands
- **Agent** — the FieldKit automation system running on a Mac Mini via OpenClaw; monitors the inbox and sends acknowledgements

---

## Configuration Interface

Each client deployment provides these values. The agent does not function without them.

| Parameter | Description | Default |
|-----------|-------------|---------|
| `agent_email` | Dedicated Gmail address the agent monitors | — |
| `admin_allowlist` | One or more email addresses permitted to send to the agent | — |
| `telegram_bot_token` | Token for the Telegram bot used to send acknowledgements | — |
| `admin_telegram_chat_id` | Admin's Telegram chat ID | — |
| `polling_interval_minutes` | How often the agent checks for new email | `5` |

---

## Core Behavior

### 1. Polling Cycle

The agent checks the Gmail inbox every `polling_interval_minutes`.

- All unread emails since the last check are processed in each cycle
- A single email failing to process does not stop the rest — the agent logs the error and continues
- Each cycle completion is logged with timestamp and counts (processed, rejected)

### 2. Sender Validation

For each new email, the agent checks the sender against `admin_allowlist`.

- **Allowlisted sender** — proceed to receipt processing
- **Unknown sender** — send a Telegram rejection notification to the admin (see format below); mark the email as read in Gmail (prevents it recurring in future cycles); log the rejection; no reply to the sender

### 3. Receipt Processing

For each valid email, in order:

1. Assign a unique reference ID — format `#NNNN`, auto-incrementing integer, zero-padded to 4 digits, persistent across agent restarts
2. Log the receipt locally: timestamp, sender, subject, attachment count, reference ID
3. Send Telegram acknowledgement to admin (see format below)
4. Mark the email as read in Gmail and apply the label `fk-received`

### 4. Telegram Acknowledgement Format

```
✓ Email received
From: admin@example.com
Subject: Before/after photos — Job #42
Received: 2026-05-11 14:32
Attachments: 3
Ref: #0014
```

### 5. Telegram Rejection Notification Format

Sent to admin when an unknown sender is detected. No reply is sent to the unknown sender.

```
✗ Email rejected — not in allowlist
From: unknown@example.com
Subject: Hi there
```

**Why notify the admin but not reply to the sender:** The admin needs visibility into rejections (e.g. they sent from the wrong address). Replying to the sender would reveal the agent address to spammers.

### 6. On-Demand Trigger

The admin sends `/check-email` via Telegram to trigger an immediate inbox check outside the polling cycle.

The agent responds with:
- One acknowledgement message per new valid email found (same format as above), or
- `No new emails.` if no new valid emails are present

### 7. Undelivered Notification Alert

The agent does not attempt real-time retry or email fallback for Telegram failures — these failure modes are rare and complex to detect reliably. Instead, a dead-letter queue approach is used:

1. Before sending each Telegram acknowledgement, the agent writes the message to a local `pending.json` queue (ref ID, recipient, timestamp, content)
2. The Telegram send is attempted (fire-and-forward — no delivery confirmation required)
3. On each polling cycle, the agent checks `pending.json` for entries older than 15 minutes (3 polling cycles at the default interval)
4. If stale entries are found: the agent sends a single alert email to the admin listing the possibly-undelivered ref IDs, then clears those entries from `pending.json`
5. The admin checks their Telegram history or runs `/check-email` to confirm what was received

The alert email is sent from the agent Gmail address to the first address in `admin_allowlist`.

### 8. Local Logging

Every event is appended to a local log on the Mac Mini:

| Event | Fields logged |
|-------|--------------|
| Valid email received | Timestamp, sender, subject, attachment count, ref ID |
| Invalid sender rejected | Timestamp, sender, subject |
| Stale notification alert sent | Timestamp, count of stale entries, ref IDs |
| Polling cycle completed | Timestamp, emails processed, emails rejected |

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Gmail unreachable | Log error, skip cycle, retry at next polling interval |
| Telegram unreachable | Notification written to pending.json; stale alert email sent to admin if undelivered for >15 minutes |
| Stale alert email fails | Log failure, no further action — admin will notice silence |
| Agent process restarts | Reference ID counter, processed map, and pending queue persist; no emails lost or double-processed |

---

## Testing Requirements

Implementation must follow test-driven development. Tests are written before or alongside code, never after.

### Unit Tests (external dependencies mocked)

- Valid email from allowlisted sender is processed correctly
- Email from unknown sender is rejected silently and logged
- Reference ID increments correctly and persists across restarts
- Telegram acknowledgement message is formatted correctly
- Stale pending.json entries trigger a single alert email to the admin
- Polling cycle processes all unread emails, not just the first
- A single failing email does not stop processing of remaining emails in the cycle

### Integration Tests (real or realistic test doubles)

- Full flow: email arrives → Telegram ack received by admin within one polling cycle
- Full flow: admin sends `/check-email` → immediate ack, no wait for polling
- Full flow: Telegram unavailable → stale alert email sent to admin after 15 minutes
- Unknown sender email → Telegram rejection notification sent to admin, rejection logged locally, email marked as read
- Agent restart → no emails lost, no emails double-processed, ref ID counter intact

---

## Success Criteria

- [ ] Admin sends email to agent address → Telegram acknowledgement arrives within one polling cycle (≤5 minutes)
- [ ] Admin sends `/check-email` via Telegram → immediate acknowledgement without waiting for polling
- [ ] Multiple emails in one polling cycle are each acknowledged individually
- [ ] Email from non-allowlisted sender → Telegram rejection notification sent to admin, logged locally, email marked as read, no reply to sender
- [ ] Telegram unavailable → admin receives stale alert email listing undelivered ref IDs after 15 minutes
- [ ] Agent restart does not reset reference ID counter or reprocess already-handled emails
- [ ] All events (receipts, rejections, stale alerts, polling cycles) are logged locally with timestamps
- [ ] No email credentials, tokens, or allowlist data are hardcoded — all come from configuration
