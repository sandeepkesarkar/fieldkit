# 001 — Email Agent: Sequence Diagram

**Last Updated:** 2026-05-09
**Source of truth:** [`techplan.md`](techplan.md) + [`clarify.md`](clarify.md)

> This diagram is generated from the spec. If the spec changes, regenerate using `/speckit.diagram`.

---

```mermaid
sequenceDiagram
    participant Admin
    participant Telegram as Telegram<br/>(OpenClaw channel)
    participant OpenClaw
    participant gws as gws CLI
    participant Gmail as Gmail API
    participant state as state.py
    participant logger as logger.py

    note over OpenClaw: Trigger: cron every POLLING_INTERVAL_MINUTES<br/>OR admin sends /check-email

    alt Admin sends /check-email
        Admin->>Telegram: /check-email
        Telegram->>OpenClaw: invoke check-email skill
    else Cron fires
        OpenClaw->>OpenClaw: scheduled cron fires
    end

    note over OpenClaw,state: STARTUP — resolve fk-received label ID once per run

    OpenClaw->>state: get_label_id()
    state-->>OpenClaw: label_id or None

    opt Label ID not cached
        OpenClaw->>gws: gmail users labels list
        gws->>Gmail: GET /users/me/labels
        Gmail-->>gws: label list
        gws-->>OpenClaw: JSON
        OpenClaw->>OpenClaw: find or create "fk-received" label
        OpenClaw->>state: save_label_id(label_id)
    end

    note over OpenClaw,state: STALE CHECK — alert admin if any notifications undelivered >15 min

    OpenClaw->>state: get_stale_pending(threshold_minutes=15)
    state-->>OpenClaw: stale entries (may be empty)

    opt Stale entries found
        OpenClaw->>gws: gmail +send --to ADMIN_EMAIL<br/>--subject "⚠️ FieldKit: Possible undelivered notifications"
        gws->>Gmail: send alert email
        Gmail-->>Admin: alert email listing stale ref IDs
        OpenClaw->>state: dequeue_pending(ref_id) for each stale entry
        OpenClaw->>logger: log_stale_alert(count, ref_ids)
    end

    note over OpenClaw,Gmail: FETCH — list unread emails not yet labelled

    OpenClaw->>gws: messages list --q "is:unread -label:fk-received"
    gws->>Gmail: GET /users/me/messages?q=...
    Gmail-->>gws: message IDs (may be empty)
    gws-->>OpenClaw: JSON list

    alt No unread messages
        note over OpenClaw: If trigger was /check-email: respond "No new emails."
        note over OpenClaw: If trigger was cron: silent — no Telegram message sent
        opt Trigger was /check-email
            OpenClaw->>Telegram: "No new emails."
            Telegram-->>Admin: No new emails.
        end
    else One or more messages found
        loop For each message
            OpenClaw->>gws: messages get --id MESSAGE_ID --format full
            gws->>Gmail: GET /users/me/messages/MESSAGE_ID
            Gmail-->>gws: full message (headers, metadata)
            gws-->>OpenClaw: JSON

            OpenClaw->>OpenClaw: extract From: header<br/>strip display name → bare address<br/>lowercase + trim

            alt Sender NOT in ADMIN_ALLOWLIST
                OpenClaw->>Telegram: ✗ Email rejected — not in allowlist\nFrom: sender@example.com\nSubject: ...
                Telegram-->>Admin: rejection notification
                OpenClaw->>gws: messages modify --removeLabelIds UNREAD<br/>(best-effort — prevents recurrence)
                OpenClaw->>logger: log_rejected(from, subject)
                note over OpenClaw: skip to next message<br/>no reply sent to unknown sender<br/>no ref ID consumed

            else Sender in ADMIN_ALLOWLIST
                OpenClaw->>state: get_ref_id_for_message(gmail_message_id)
                state->>state: check processed map

                alt Message ID already in processed map (crash recovery)
                    state-->>OpenClaw: existing ref ID (e.g. #0014)
                else New message ID
                    state->>state: increment last_ref_id<br/>record gmail_message_id → ref_id<br/>write state.json
                    state-->>OpenClaw: new ref ID (e.g. #0015)
                end

                OpenClaw->>state: enqueue_pending(ref_id, gmail_message_id, from, subject)

                OpenClaw->>Telegram: ✓ Email received\nFrom: admin@example.com\nSubject: ...\nReceived: YYYY-MM-DD HH:MM\nAttachments: N\nRef: #NNNN
                Telegram-->>Admin: acknowledgement (delivery not confirmed)

                OpenClaw->>state: dequeue_pending(ref_id)

                OpenClaw->>logger: log_received(from, subject, attachments, ref_id)

                OpenClaw->>gws: messages modify --removeLabelIds UNREAD<br/>--addLabelIds LABEL_ID<br/>(best-effort)
                gws->>Gmail: PATCH /users/me/messages/MESSAGE_ID/modify
                note over gws,Gmail: failure here is non-fatal —<br/>state.json processed map handles deduplication
            end
        end

        OpenClaw->>logger: log_cycle(processed=N, rejected=M)
    end
```

---

## Error Paths Not Shown Above

| Scenario | Behavior |
|----------|----------|
| Gmail unreachable | `gws messages list` fails → log error, skip cycle, retry at next cron interval |
| Telegram unreachable | Message stays in `pending.json`; stale alert email sent to admin after 15 min |
| Stale alert email fails | Log failure, no further action |
| `gws modify` fails (label/mark-read) | Non-fatal — `state.json` processed map prevents reprocessing |
| `state.json` missing | `state.py` creates it with defaults |
| `pending.json` missing | `state.py` creates it with empty pending array |
