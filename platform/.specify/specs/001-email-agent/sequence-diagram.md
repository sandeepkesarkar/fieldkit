# 001 — Email Agent: Sequence Diagram

**Last Updated:** 2026-05-10
**Source of truth:** [`techplan.md`](techplan.md) + [`clarify.md`](clarify.md)

> This diagram is generated from the spec. If the spec changes, regenerate using `/speckit.diagram`.

---

```mermaid
sequenceDiagram
    participant Admin
    participant Telegram as Telegram<br/>(OpenClaw channel)
    participant OpenClaw
    participant Script as check_email.py
    participant gws as gws CLI
    participant Gmail as Gmail API
    participant state as state.py
    participant logger as logger.py

    note over OpenClaw,Script: Trigger: system cron every POLLING_INTERVAL_MINUTES<br/>OR admin sends /check_email

    alt Admin sends /check_email
        Admin->>Telegram: /check_email
        Telegram->>OpenClaw: route to agent
        OpenClaw->>Script: python3 scripts/check_email.py
    else System cron fires
        Script->>Script: python3 scripts/check_email.py --source cron
    end

    note over Script: STARTUP — acquire run lock

    Script->>Script: acquire run.lock (LOCK_EX | LOCK_NB)<br/>exit(0) silently if another instance is running

    note over Script,state: STARTUP — resolve fk-received label ID once per run

    Script->>state: get_label_id()
    state-->>Script: label_id or None

    opt Label ID not cached
        Script->>gws: gmail users labels list
        gws->>Gmail: GET /users/me/labels
        Gmail-->>gws: label list
        gws-->>Script: JSON
        Script->>Script: find or create "fk-received" label
        Script->>state: save_label_id(label_id)
    end

    note over Script,state: STALE CHECK — alert admin if any notifications undelivered >15 min

    Script->>state: get_stale_pending(threshold_minutes=15)
    state-->>Script: stale entries (may be empty)

    opt Stale entries found
        Script->>gws: gmail users messages send<br/>(RFC 2822 base64 alert email to admin)
        gws->>Gmail: POST /users/me/messages/send
        Gmail-->>Admin: alert email listing stale ref IDs
        Script->>state: dequeue_pending(ref_id) for each stale entry
        Script->>logger: log_stale_alert(ref_ids)
    end

    note over Script,Gmail: FETCH — list unread emails not yet labelled

    Script->>gws: messages list --q "is:unread -label:fk-received"
    gws->>Gmail: GET /users/me/messages?q=...
    Gmail-->>gws: message IDs (may be empty)
    gws-->>Script: JSON list

    alt No unread messages
        opt User-triggered (/check_email) — not cron
            Script->>Telegram: openclaw message send "No new emails."
            Telegram-->>Admin: No new emails.
        end
        note over Script: Cron trigger: silent — no Telegram message sent
    else One or more messages found
        loop For each message
            Script->>gws: messages get --id MESSAGE_ID --format full
            gws->>Gmail: GET /users/me/messages/MESSAGE_ID
            Gmail-->>gws: full message (headers, metadata)
            gws-->>Script: JSON

            Script->>Script: extract From: header<br/>strip display name → bare address<br/>lowercase + trim

            alt Sender NOT in ADMIN_ALLOWLIST
                Script->>Telegram: openclaw message send<br/>"✗ Email rejected — not in allowlist\nFrom: …\nSubject: …"
                Telegram-->>Admin: rejection notification
                Script->>gws: messages modify --removeLabelIds UNREAD<br/>(best-effort — prevents recurrence)
                Script->>logger: log_rejected(from, subject)
                note over Script: skip to next message<br/>no reply to unknown sender<br/>no ref ID consumed

            else Sender in ADMIN_ALLOWLIST
                Script->>state: get_ref_id_for_message(gmail_message_id)
                state->>state: check processed map

                alt Message ID already in processed map (crash recovery)
                    state-->>Script: existing ref ID (e.g. #0014)
                else New message ID
                    state->>state: increment last_ref_id<br/>record gmail_message_id → ref_id<br/>write state.json
                    state-->>Script: new ref ID (e.g. #0015)
                end

                Script->>state: enqueue_pending(ref_id, gmail_message_id, from, subject)

                Script->>Telegram: openclaw message send<br/>"✓ Email received\nFrom: …\nSubject: …\nReceived: …\nAttachments: N\nRef: #NNNN"
                Telegram-->>Admin: acknowledgement (delivery not confirmed)

                Script->>state: dequeue_pending(ref_id)
                Script->>logger: log_received(from, subject, attachments, ref_id)

                Script->>gws: messages modify --removeLabelIds UNREAD<br/>--addLabelIds LABEL_ID<br/>(best-effort)
                gws->>Gmail: PATCH /users/me/messages/MESSAGE_ID/modify
                note over gws,Gmail: failure here is non-fatal —<br/>state.json processed map handles deduplication
            end
        end

    end

    Script->>logger: log_cycle(processed=N, rejected=M)
    Script->>Script: release run.lock
```

---

## Error Paths Not Shown Above

| Scenario | Behavior |
|----------|----------|
| Gmail unreachable | `gws messages list` fails → Telegram error sent, script exits 1, retry at next cron interval |
| Telegram unreachable | Message stays in `pending.json`; stale alert email sent to admin after 15 min |
| Stale alert email fails | Log warning, dequeue and log stale entries regardless |
| `gws modify` fails (label/mark-read) | Non-fatal — `state.json` processed map prevents reprocessing |
| `state.json` missing | `state.py` creates it with defaults |
| `pending.json` missing | `state.py` creates it with empty pending array |
| Concurrent run (cron + manual overlap) | Second instance detects `run.lock` held, exits 0 silently |
