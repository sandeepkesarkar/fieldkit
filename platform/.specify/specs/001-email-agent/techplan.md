# 001 — Email Agent: Technical Plan

**Status:** Technical Planning
**Spec:** [`spec.md`](spec.md)
**Clarifications:** [`clarify.md`](clarify.md)
**Last Updated:** 2026-05-09

---

## Stack

| Concern | Solution | Rationale |
|---------|----------|-----------|
| Runtime & orchestration | OpenClaw | Handles agent loop, scheduling, Telegram channel |
| Gmail operations | `gws` CLI | Agent-designed, structured JSON output, no custom API code |
| Telegram delivery | OpenClaw Telegram channel | Native channel — no bot code or token management in the skill |
| Polling / scheduling | OpenClaw cron | Built-in, persists across restarts, retries on failure |
| Ref ID persistence | `state.py` + `state.json` | Simple counter, no database needed |
| Local logging | Append-only log file | Plain text, pipe-delimited |
| Testing | `pytest` + `pytest-mock` | Standard Python testing |

---

## Components

### 1. OpenClaw Cron Job

Triggers an isolated agent run every `polling_interval_minutes` (default 5). Registered once during setup:

```bash
openclaw cron add \
  --name "email-agent-poll" \
  --cron "*/${POLLING_INTERVAL_MINUTES} * * * *" \
  --session isolated \
  --message "Check Gmail inbox for new emails and process them per the email agent skill"
```

The cron expression is computed from `POLLING_INTERVAL_MINUTES` in `.env` — do not hardcode `*/5`. Load the variable before running this command: `source .env && openclaw cron add ...`. If the interval changes, delete and re-register the cron job.

### 2. `/check-email` Skill (`SKILL.md`)

User-invocable OpenClaw skill. Admin sends `/check-email` in Telegram → triggers an immediate inbox check outside the polling cycle. Same processing logic as the cron-triggered run.

```markdown
---
name: check-email
description: Check Gmail inbox for new emails and send Telegram acknowledgements for each valid email received
user-invocable: true
metadata: {"openclaw": {"requires": {"bins": ["gws"], "env": ["AGENT_EMAIL", "ADMIN_ALLOWLIST"]}}}
---
```

### 3. Gmail Operations via `gws`

All Gmail interaction uses `gws` CLI commands. Output is structured JSON consumed by the OpenClaw agent.

| Operation | Command |
|-----------|---------|
| List unprocessed emails | `gws gmail users messages list --q "is:unread -label:fk-received"` |
| Get email details | `gws gmail users messages get --id MESSAGE_ID --format full` |
| Mark as read | `gws gmail users messages modify --id MESSAGE_ID --removeLabelIds UNREAD` |
| Apply processed label | `gws gmail users messages modify --id MESSAGE_ID --addLabelIds fk-received` |
| Resolve label ID (startup) | `gws gmail users labels list` → find or create `fk-received`, cache ID in `state.json` |
| Send stale alert email | `gws gmail +send --to ADMIN_EMAIL --subject "⚠️ FieldKit: Possible undelivered notifications" --body "..."` |

`gws` auth is configured once at setup (`gws auth login`) and persists in the OS keyring.

**Note:** `gws modify --addLabelIds` requires a label ID, not a label name. The label ID is resolved once at startup via `gws gmail users labels list` and stored in `state.json`. If the label does not exist, it is created first. This is best-effort — a label application failure does not block processing since `state.json` is the primary deduplication source.

### 4. State Manager (`tools/state.py`)

Manages the ref ID counter and processed message map. Reads `state.json` on call, writes back after changes.

```json
{
  "last_ref_id": 14,
  "processed": {
    "18f3a2b1c4d5e6f7": "#0014"
  }
}
```

Functions:
- `get_ref_id_for_message(gmail_message_id) -> str` — returns existing ref ID if already seen, otherwise increments, records, returns new ID
- `read_last_ref_id() -> int` — read-only inspection
- `get_label_id() -> str | None` — returns cached `fk-received` label ID from `state.json`
- `save_label_id(label_id) -> None` — caches the resolved label ID

**pending.json** lives alongside `state.json` at `~/fieldkit/data/email-agent/pending.json` and is also managed via `state.py`:
- `enqueue_pending(ref_id, gmail_message_id, from_addr, subject) -> None`
- `dequeue_pending(ref_id) -> None`
- `get_stale_pending(threshold_minutes=15) -> list` — returns entries older than threshold

### 5. Local Logger (`tools/logger.py`)

Appends to `~/fieldkit/logs/email-agent.log`. One line per event.

```
2026-05-09 14:32 | RECEIVED     | from=admin@example.com subject="Job #42" attachments=3 ref=#0014
2026-05-09 14:33 | REJECTED     | from=unknown@example.com subject="Offer"
2026-05-09 14:33 | STALE_ALERT  | count=2 refs=#0012,#0013
2026-05-09 14:35 | CYCLE        | processed=1 rejected=1
```

---

## Data Flow

### Happy Path (cron or `/check-email`)

```
0. STARTUP (once per agent run):
   a. state.py → get_label_id()
   b. If no label ID cached: gws gmail users labels list → find "fk-received"
   c. If not found: create it, save ID via state.py → save_label_id()

1. STALE CHECK (before processing emails):
   a. state.py → get_stale_pending(threshold_minutes=15)
   b. If stale entries found:
      - gws gmail +send → alert email to ADMIN_ALLOWLIST[0] listing ref IDs
      - state.py → dequeue_pending for each stale entry
      - logger.py → log_stale_alert(count, ref_ids)

2. FETCH: gws gmail users messages list --q "is:unread -label:fk-received"

3. For each message returned:
   a. gws gmail users messages get --id MESSAGE_ID
   b. Extract sender from From: header (strip display name, lowercase, trim)
   c. Check sender against ADMIN_ALLOWLIST
      → Not in allowlist:
         i.   Send Telegram rejection notification via OpenClaw
         ii.  gws modify → mark as read (best-effort, prevents recurrence)
         iii. logger.py → log_rejected(from, subject)
         iv.  Skip to next message
      → In allowlist:
         i.   state.py → get_ref_id_for_message(gmail_message_id) → assign or reuse ref ID
         ii.  state.py → enqueue_pending(ref_id, gmail_message_id, from, subject)
         iii. Send Telegram ack via OpenClaw channel
         iv.  state.py → dequeue_pending(ref_id)
         v.   logger.py → log_received(from, subject, attachments, ref_id)
         vi.  gws modify → mark read + apply fk-received label ID (best-effort)

4. logger.py → log_cycle(processed, rejected)
```

### Telegram Acknowledgement Format

Delivered via OpenClaw's Telegram channel (not raw Bot API):

```
✓ Email received
From: admin@example.com
Subject: Before/after photos — Job #42
Received: 2026-05-09 14:32
Attachments: 3
Ref: #0014
```

---

## File Structure

```
platform/email-agent/
  SKILL.md                 # OpenClaw skill definition (/check-email)
  tools/
    state.py               # ref ID counter — reads/writes state.json
    logger.py              # appends to email-agent.log
  tests/
    test_state.py
    test_logger.py
  .env.example             # template — copy to .env on Mac Mini, never commit
```

**Runtime data on Mac Mini (not committed):**

```
~/fieldkit/
  data/email-agent/
    state.json             # ref ID counter, processed map, label ID cache (gitignored)
    pending.json           # undelivered notification queue (gitignored)
  logs/
    email-agent.log        # append-only event log (gitignored)
```

---

## Configuration (`.env`)

| Variable | Description | Example |
|----------|-------------|---------|
| `AGENT_EMAIL` | Gmail address the agent monitors | `fieldkit.agent@gmail.com` |
| `ADMIN_ALLOWLIST` | Comma-separated permitted senders | `admin@mybusiness.com` |
| `POLLING_INTERVAL_MINUTES` | Polling frequency | `5` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | — verify if managed at OpenClaw level |
| `ADMIN_TELEGRAM_CHAT_ID` | Admin's Telegram chat ID | — verify if managed at OpenClaw level |

Note: `TELEGRAM_BOT_TOKEN` and `ADMIN_TELEGRAM_CHAT_ID` may be configured at the OpenClaw channel level rather than in the skill. Confirm during setup — if so, remove them from `.env`.

---

## Setup Checklist (Mac Mini)

Steps required before the agent can run. Not implementation tasks — these are one-time environment setup:

- [ ] OpenClaw installed and running
- [ ] OpenClaw Telegram channel configured with admin account
- [ ] `gws` installed: `brew install googleworkspace-cli`
- [ ] `gws` authenticated: `gws auth login --account $AGENT_EMAIL`
- [ ] Gmail label `fk-received` created in agent Gmail account
- [ ] Runtime directories created: `mkdir -p ~/fieldkit/data/email-agent ~/fieldkit/logs`
- [ ] `.env` created from `.env.example` and populated
- [ ] OpenClaw cron job registered (command in Component 1 above)

---

## Deferred

**`gws gmail +watch` — event-driven triggering**

Would replace the cron poll with instant delivery via Google PubSub. Deferred to a later phase. Migration path documented in `clarify.md` — processing logic is unchanged, only the trigger mechanism swaps out.
