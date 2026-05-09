# 001 — Email Agent: Task Breakdown

**Status:** Task Breakdown
**Techplan:** [`techplan.md`](techplan.md)
**Last Updated:** 2026-05-09

Tasks are ordered by dependency. T01–T03 are independent and can be done in any order. T04 depends on T01–T03. T05–T07 are sequential.

---

## Definition of Done

A task is complete **only** when its tests pass. No exceptions.

| Task type | Test requirement |
|-----------|-----------------|
| Code (T01, T02) | `pytest` run clean — zero failures, zero errors |
| Config (T03) | Manually verified: all variables present, file loads without error |
| Skill (T04) | `openclaw skills list` shows `check-email` with no load errors |
| Setup (T05) | `gws gmail users messages list --q "is:unread"` returns valid response |
| Smoke test (T06, T07) | Every numbered step in the task verified and checked off |

**Both the implementer and the assisting AI must run the tests before marking a task done.** If a test fails after implementation, the task is not done — fix and re-run before moving to the next task.

**Gate rule:** Do not start T04 until `pytest tests/` is clean across T01 and T02. Do not start T05 until T04 is verified in OpenClaw. Do not start T06 until T05 checklist is fully checked off.

---

## Code Standards (applies to all code tasks)

### Comments

Every module must have a module-level docstring stating its purpose and the file paths it reads/writes. Every public function must have a one-line docstring. Non-obvious logic (locking protocol, zero-padding, timestamp format, deduplication logic) must have an inline comment explaining **why**, not what.

### Logging

All Python modules must use the standard `logging` module with a module-level logger (`logger = logging.getLogger(__name__)`). Log levels:

| Level | When to use |
|-------|-------------|
| `DEBUG` | Internal state transitions — file reads, map lookups, lock acquired/released |
| `INFO` | Meaningful state changes — new ref ID assigned, label ID cached, entry enqueued/dequeued |
| `WARNING` | Unexpected-but-handled conditions — file missing and being created, stale entries found |
| `ERROR` | Failures that prevent an operation from completing |

**Sensitive data must never appear in log output.** This is a hard rule with no exceptions.

| Field | Classification | Log policy |
|-------|---------------|------------|
| Email addresses (`from_addr`) | PII | Never log — use `"<redacted>"` or omit entirely |
| Subject lines | Potentially sensitive | Never log |
| Email body content | Sensitive | Never log |
| Gmail message IDs | Internal opaque ID | Safe to log at DEBUG |
| Ref IDs (`#NNNN`) | Internal tracking ID | Safe to log |
| Label IDs | Internal Google ID | Safe to log |
| File paths | Internal | Safe to log |
| Counts and timestamps | Non-sensitive | Safe to log |

Log configuration (level, handlers, format) is the caller's responsibility — modules must not call `logging.basicConfig()` or add handlers. This keeps log output under the control of the runtime (OpenClaw or the test runner).

---

## T01 — `tools/state.py` + unit tests + `requirements.txt`

**What:** Ref ID counter. Reads `state.json`, increments, writes back, returns formatted ID.

**Functions to implement:**

*Ref ID management:*
- `get_ref_id_for_message(gmail_message_id: str) -> str` — checks `processed` map; if seen, returns existing ID; if new, increments, records, returns zero-padded ID e.g. `#0015`
- `read_last_ref_id() -> int` — read-only inspection

*Label ID cache:*
- `get_label_id() -> str | None` — returns cached `fk-received` label ID or None
- `save_label_id(label_id: str) -> None` — writes label ID to `state.json`

*Pending queue (pending.json):*
- `enqueue_pending(ref_id, gmail_message_id, from_addr, subject) -> None` — adds entry with current UTC timestamp
- `dequeue_pending(ref_id) -> None` — removes entry by ref_id
- `get_stale_pending(threshold_minutes=15) -> list` — returns entries older than threshold

**State files:** `~/fieldkit/data/email-agent/state.json` and `~/fieldkit/data/email-agent/pending.json`

**state.json schema:**
```json
{
  "last_ref_id": 14,
  "processed": { "18f3a2b1c4d5e6f7": "#0014" },
  "fk_received_label_id": "Label_12345678"
}
```

**pending.json schema:**
```json
{
  "pending": [
    { "ref_id": "#0014", "gmail_message_id": "...", "from": "...", "subject": "...", "queued_at": "2026-05-09T14:32:00Z" }
  ]
}
```

**Tests to write (`tests/test_state.py`):**
- New message ID starts at `#0001` when state.json doesn't exist
- New message ID increments correctly across consecutive calls
- Known message ID returns existing ref ID without incrementing counter
- Zero-pads to 4 digits (`#0001`, `#0099`, `#1000`)
- `get_label_id()` returns None when not cached; returns value after `save_label_id()`
- `enqueue_pending()` adds entry with correct timestamp
- `dequeue_pending()` removes only the matching ref_id entry
- `get_stale_pending()` returns entries older than threshold, ignores fresh ones
- All functions create their respective files if they don't exist
- Concurrent calls to `get_ref_id_for_message` from two threads produce unique ref IDs (file locking test)

**File locking:** Every function that reads and writes `state.json` or `pending.json` must use `fcntl.flock(fd, fcntl.LOCK_EX)` before reading and `fcntl.LOCK_UN` after writing. This prevents data corruption from concurrent cron + `/check-email` runs. See `clarify.md` (File Locking section).

**Also create:** `requirements.txt` with `pytest` and `pytest-mock`.

**Done when:** `pytest tests/test_state.py` passes.

---

## T02 — `tools/logger.py` + unit tests

**What:** Append-only local event log. One line per event, pipe-delimited.

**Functions to implement:**
- `log_received(from_addr, subject, attachments, ref_id)` → `RECEIVED` line
- `log_rejected(from_addr, subject)` → `REJECTED` line
- `log_stale_alert(count, ref_ids)` → `STALE_ALERT` line
- `log_cycle(processed, rejected)` → `CYCLE` line

**Log file location:** `~/fieldkit/logs/email-agent.log`
Creates the directory if it doesn't exist.

**Log format (from techplan):**
```
2026-05-09 14:32 | RECEIVED     | from=admin@example.com subject="Job #42" attachments=3 ref=#0014
2026-05-09 14:33 | REJECTED     | from=unknown@example.com subject="Offer"
2026-05-09 14:33 | STALE_ALERT  | count=2 refs=#0012,#0013
2026-05-09 14:35 | CYCLE        | processed=1 rejected=1
```

**Tests to write (`tests/test_logger.py`):**
- Each function produces the correct line format
- `log_received` does NOT include a channel field
- `log_stale_alert` formats ref_ids as comma-separated list
- Appends to existing file (does not overwrite)
- Creates log directory if it doesn't exist
- Timestamp format is `YYYY-MM-DD HH:MM`

**Done when:** `pytest tests/test_logger.py` passes.

---

## T03 — `.env.example`

**What:** Config template. Documents every required variable with description and example value.

**Variables to include** (from techplan):

| Variable | Description | Example |
|----------|-------------|---------|
| `AGENT_EMAIL` | Gmail address the agent monitors | `fieldkit.agent@gmail.com` |
| `ADMIN_ALLOWLIST` | Comma-separated permitted senders | `admin@mybusiness.com` |
| `POLLING_INTERVAL_MINUTES` | Polling frequency in minutes | `5` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token — verify if managed at OpenClaw level | |
| `ADMIN_TELEGRAM_CHAT_ID` | Admin's Telegram chat ID — verify if managed at OpenClaw level | |

**Done when:** File exists with all variables documented and a comment at the top explaining it must be copied to `.env` and never committed.

---

## T04 — `SKILL.md`

**What:** OpenClaw skill definition. Contains the frontmatter gates and the full agent instructions — this is the processing logic.

**Frontmatter:**
```markdown
---
name: check-email
description: Check Gmail inbox for new emails and send Telegram acknowledgements for each valid email received
user-invocable: true
metadata: {"openclaw": {"requires": {"bins": ["gws"], "env": ["AGENT_EMAIL", "ADMIN_ALLOWLIST"]}}}
---
```

**Body must instruct the agent to follow the full data flow from `techplan.md`:**
0. Startup: resolve `fk-received` label ID via `state.py get_label_id()`; if missing, run `gws gmail users labels list`, find or create label, call `state.py save_label_id()`
1. Stale check: call `state.py get_stale_pending(15)`; if results: send alert email via `gws gmail +send` to first ADMIN_ALLOWLIST entry, call `state.py dequeue_pending()` for each, call `logger.py log_stale_alert()`
2. Run `gws gmail users messages list --q "is:unread -label:fk-received"`
3. For each message: get details, check processed map via `state.py get_ref_id_for_message()`, check sender against `ADMIN_ALLOWLIST`
4. Invalid sender: send Telegram rejection notification, mark email as read via `gws modify` (best-effort), call `logger.py log_rejected()`
5. Valid sender (new message): call `state.py enqueue_pending()`, send Telegram ack, call `state.py dequeue_pending()`, call `logger.py log_received()`, mark read + apply label via `gws modify` (best-effort)
6. After all messages: call `logger.py log_cycle()` with counts
7. If no new emails AND trigger was `/check-email`: respond `No new emails.`
   If no new emails AND trigger was cron: silent — no Telegram message sent

**Done when:** Skill loads in OpenClaw without errors (`openclaw skills list` shows `check-email`).

---

## T05 — Mac Mini environment setup

**What:** One-time environment setup. Follow the setup checklist in `techplan.md` exactly.

**Checklist (from techplan):**
- [ ] OpenClaw installed and running
- [ ] OpenClaw Telegram channel configured with admin account
- [ ] `gws` installed: `brew install googleworkspace-cli`
- [ ] `gws` authenticated: `gws auth login --account $AGENT_EMAIL`
- [ ] ~~Gmail label `fk-received` created in agent Gmail account~~ — **not needed**: agent creates it automatically on first run via `gws gmail users labels list` + create if missing
- [ ] Runtime directories created: `mkdir -p ~/fieldkit/data/email-agent ~/fieldkit/logs`
- [ ] `.env` created from `.env.example` and populated
- [ ] Confirm whether `TELEGRAM_BOT_TOKEN` / `ADMIN_TELEGRAM_CHAT_ID` are needed in `.env` or are managed at OpenClaw level — update `.env.example` accordingly

**Done when:** All checklist items checked off. `gws gmail users messages list --q "is:unread"` returns a valid response (including an empty list) from the agent Gmail account.

---

## T06 — Smoke test: `/check-email` manual trigger

**What:** First end-to-end test. Verifies the full happy path works before enabling automated polling.

**Steps:**
1. Send a test email from an allowlisted address to the agent Gmail account
2. Send `/check-email` via Telegram
3. Verify Telegram ack arrives with correct format (ref, subject, from, attachments, timestamp)
4. Verify `state.json` incremented
5. Verify `email-agent.log` has a `RECEIVED` line and a `CYCLE` line
6. Verify email is marked read in Gmail and has label `fk-received`

**Also verify rejection path:**
7. Send an email from a non-allowlisted address
8. Send `/check-email`
9. Verify Telegram rejection notification arrives (not a receipt ack)
10. Verify email is marked as read in Gmail
11. Verify `email-agent.log` has a `REJECTED` line

**Also verify stale alert path:**
12. Manually add a stale entry to `pending.json` (timestamp >15 min in the past)
13. Trigger a polling cycle or `/check-email`
14. Verify alert email arrives in admin inbox listing the stale ref ID
15. Verify stale entry is removed from `pending.json`
16. Verify `email-agent.log` has a `STALE_ALERT` line

**Also verify `gws gmail +send` works:**
17. Confirm `gws gmail +send --to ADMIN_EMAIL --subject "test" --body "test"` delivers successfully
(This validates the stale alert delivery mechanism independently)

**Done when:** All 17 steps pass.

---

## T07 — Smoke test: cron poll

**What:** Registers the OpenClaw cron job and verifies automated polling works without manual trigger.

**Steps:**
1. Register cron job (command from techplan Component 1)
2. Verify it appears in `openclaw cron list`
3. Send a test email from an allowlisted address
4. Wait up to `POLLING_INTERVAL_MINUTES + 1` minutes
5. Verify Telegram ack arrives without issuing `/check-email`
6. Verify log and state.json updated

**Done when:** All 6 steps pass. Agent is live.

---

## Summary

| Task | Depends on | Type | Done when |
|------|-----------|------|-----------|
| T01 — `state.py` + tests | — | Code | `pytest tests/test_state.py` passes |
| T02 — `logger.py` + tests | — | Code | `pytest tests/test_logger.py` passes |
| T03 — `.env.example` | — | Config | All variables present, verified manually |
| **── gate: `pytest tests/` clean ──** | | | |
| T04 — `SKILL.md` | T01, T02, T03 | Skill | `openclaw skills list` shows `check-email` |
| **── gate: T04 verified in OpenClaw ──** | | | |
| T05 — Mac Mini setup | T03, T04 | Setup | `gws messages list` returns valid response |
| **── gate: T05 checklist fully checked ──** | | | |
| T06 — Smoke test: manual | T05 | Test | All 17 steps pass |
| T07 — Smoke test: cron | T06 | Test | All 6 steps pass — agent is live |
