---
name: check-email
description: Check Gmail inbox for new emails and send Telegram acknowledgements for each valid email received
user-invocable: true
metadata: {"openclaw": {"requires": {"bins": ["gws"], "env": ["AGENT_EMAIL", "ADMIN_ALLOWLIST"]}}}
---

# check-email

Polls the agent Gmail inbox for unread, unlabeled messages. Valid messages (senders in `ADMIN_ALLOWLIST`) receive a Telegram acknowledgement and are labeled `fk-received`. Invalid senders receive a rejection notification. Stale pending entries trigger an alert email to the admin before processing begins.

**Run from:** `~/fieldkit/platform/email-agent/`

**Python tools:** import from `tools/state.py` and `tools/logger.py` in that directory.

---

## Phase 0 — Setup

**Step 1 — Source environment**

Load `.env` from the working directory. Required variables:
- `AGENT_EMAIL` — Gmail address the agent monitors
- `ADMIN_ALLOWLIST` — comma-separated permitted sender addresses

Parse `ADMIN_ALLOWLIST`: split on commas, strip whitespace around each entry, lowercase. Store as a lookup set. Entries are matched by exact lowercased email address — non-email strings are allowed but will never match a sender.

If the parsed set is empty, abort the run and report via OpenClaw channel: `check-email: ADMIN_ALLOWLIST is empty — add at least one permitted sender address to .env`.

---

## Phase 1 — Resolve `fk-received` label

**Step 2 — Check label cache**

Call `get_label_id()` from `tools/state.py`.

- **Cached (non-None):** use the returned value as `LABEL_ID`. Skip to Phase 2.
- **Not cached (None):** continue to Step 3.

**Step 3 — Resolve via Gmail API**

Run:
```
gws gmail users labels list --userId $AGENT_EMAIL
```

Search the response for a label with `name == "fk-received"`.

- **Found:** use its `id` as `LABEL_ID`.
- **Not found:** create it:
  ```
  gws gmail users labels create --name fk-received --userId $AGENT_EMAIL
  ```
  Use the `id` from the response as `LABEL_ID`.

**Step 4 — Cache the label ID**

Call `save_label_id(LABEL_ID)` from `tools/state.py`.

---

## Phase 2 — Stale check

**Step 5 — Find overdue pending entries**

Call `get_stale_pending(threshold_minutes=15)` from `tools/state.py`.

If the result is empty, skip to Phase 3.

**Step 6 — Alert admin**

Send an alert email (log a warning and continue if the send fails — dequeue and log regardless):
```
gws gmail +send \
  --to {ADMIN_ALLOWLIST[0]} \
  --subject "⚠️ FieldKit: Possible undelivered notifications" \
  --body "These acknowledgements may not have been delivered via Telegram:\n\n{for each stale entry: Ref {ref_id} — {subject} (queued {queued_at formatted as YYYY-MM-DD HH:MM, strip T and Z from ISO string})}\n\nCheck Telegram history or send /check-email to confirm."
```

**Step 7 — Dequeue and log stale entries**

For each stale entry:
- Call `dequeue_pending(entry["ref_id"])` from `tools/state.py`.

Then call `log_stale_alert(ref_ids)` from `tools/logger.py` with the list of stale `ref_id` strings.

---

## Phase 3 — Fetch unread messages

**Step 8 — List unread, unlabeled messages**

Run:
```
gws gmail users messages list --userId $AGENT_EMAIL --q "is:unread -label:fk-received"
```

If the response contains no messages, go directly to Phase 5 with `processed = 0`, `rejected = 0`.

---

## Phase 4 — Process each message

Repeat Steps 9–16 for every `messageId` in the response. Track `processed = 0` and `rejected = 0` counters throughout.

**Step 9 — Fetch full message**

Run:
```
gws gmail users messages get --userId $AGENT_EMAIL --id $MESSAGE_ID --format full
```

**Step 10 — Extract headers and metadata**

From the response:
- `from_addr`: parse the `From:` header. Strip display name if present (`"Name" <addr>` → `addr`). Lowercase and trim. If the header is malformed or no address can be extracted, treat `from_addr` as `"unknown"` (will be rejected).
- `subject`: value of the `Subject:` header (empty string if absent).
- `received_at`: value of the `Date:` header.
- `attachments`: count of message parts that have a non-empty `filename` field.

**Step 11 — Check allowlist**

Lowercase `from_addr` and check membership in the parsed `ADMIN_ALLOWLIST` set.

---

### Rejected path (sender NOT in allowlist)

**Step 12 — Send Telegram rejection notification**

Send via OpenClaw channel:
```
✗ Email rejected — not in allowlist
From: {from_addr}
Subject: {subject}
```

**Step 13 — Mark message read (best-effort)**

Run (do not abort on failure — log a warning and continue):
```
gws gmail users messages modify --userId $AGENT_EMAIL --id $MESSAGE_ID --removeLabelIds UNREAD
```

**Step 14 — Log and count**

Call `log_rejected(from_addr, subject)` from `tools/logger.py`.

Increment `rejected`. Continue to next message.

---

### Accepted path (sender IS in allowlist)

**Step 15 — Assign ref ID and enqueue**

Call `get_ref_id_for_message(MESSAGE_ID)` from `tools/state.py`. Store result as `REF_ID`.

Call `enqueue_pending(REF_ID, MESSAGE_ID, from_addr, subject)` from `tools/state.py`.

**Step 16 — Send Telegram acknowledgement**

Send via OpenClaw channel:
```
✓ Email received
From: {from_addr}
Subject: {subject}
Received: {received_at}
Attachments: {attachments}
Ref: {REF_ID}
```

**Step 17 — Dequeue (always)**

Call `dequeue_pending(REF_ID)` from `tools/state.py`. Call this unconditionally — OpenClaw delivery cannot be observed. The stale check in Phase 2 handles the case where the notification was silently lost.

**Step 18 — Log and label**

Call `log_received(from_addr, subject, attachments, REF_ID)` from `tools/logger.py`.

Apply label and mark read (best-effort — do not abort on failure):
```
gws gmail users messages modify \
  --userId $AGENT_EMAIL \
  --id $MESSAGE_ID \
  --removeLabelIds UNREAD \
  --addLabelIds $LABEL_ID
```

Increment `processed`. Continue to next message.

---

## Phase 5 — Cycle complete

**Step 19 — Log cycle**

Call `log_cycle(processed, rejected)` from `tools/logger.py`.

**Step 20 — Reply if user-triggered with no new mail**

If this run was triggered by `/check-email` (user-invocable) **and** `processed == 0` **and** `rejected == 0`:

Send via OpenClaw channel: `No new emails.`

If triggered by cron: no reply.

---

## Error handling

| Condition | Action |
|-----------|--------|
| Gmail API error on a single message | Log the error, skip that message, do not increment either counter |
| `gws modify` failure (mark read / apply label) | Log a warning and continue — these are best-effort only |
| Alert email send fails (Step 6) | Log a warning and continue — dequeue stale entries and call `log_stale_alert` regardless |
| `RuntimeError` from `tools/state.py` (corrupt `state.json` or `pending.json`) | Abort the run; report the error message via OpenClaw channel so the operator can intervene |
