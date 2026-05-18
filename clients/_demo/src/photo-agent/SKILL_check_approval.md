---
name: check_approval
description: Check for a pending video approval response from the admin and process it
user-invocable: true
metadata: {"openclaw": {"requires": {"bins": ["python3"]}}}
---

# check_approval

## When triggered by a Telegram button press (callback_query)

When the admin presses the Approve or Reject button, Telegram sends a callback_query.
Extract three values from the callback_query and pass them as arguments:

- `callback_query.id` → `--callback-query-id`
- `callback_query.data` → `--callback-data` (will be `approve` or `reject`)
- `callback_query.message.message_id` → `--message-id`

Run:

```bash
cd ~/src/fieldkit/clients/_demo/src/photo-agent || { echo "ERROR: photo-agent directory not found"; exit 1; }
python3 scripts/check_approval.py --callback-query-id CALLBACK_QUERY_ID --callback-data CALLBACK_DATA --message-id MESSAGE_ID 2>&1
```

Replace `CALLBACK_QUERY_ID`, `CALLBACK_DATA`, and `MESSAGE_ID` with the actual values from the callback_query.

## When invoked manually (e.g. /check_approval command)

Run the script without extra args — it will poll Telegram getUpdates itself:

```bash
cd ~/src/fieldkit/clients/_demo/src/photo-agent || { echo "ERROR: photo-agent directory not found"; exit 1; }
python3 scripts/check_approval.py 2>&1
```

## Output handling

Do not interpret Telegram updates yourself. Run the script once and do not retry.
If the exit code is non-zero, report it as an error: "Script failed (exit <code>): <output>"
If the script exits with code 0 and no output, report: "No pending approval."
Otherwise relay the output verbatim. Do not summarise or paraphrase.
