---
name: check_approval
description: Check for a pending video approval response from the admin and process it
user-invocable: true
metadata: {"openclaw": {"requires": {"bins": ["python3"]}}}
---

# check_approval

## When triggered by the Approve button (callback_data == "approve")

```bash
cd ~/src/fieldkit/platform/photo-agent || { echo "ERROR: photo-agent directory not found"; exit 1; }
python3 scripts/check_approval.py --callback-data approve 2>&1
```

## When triggered by the Reject button (callback_data == "reject")

```bash
cd ~/src/fieldkit/platform/photo-agent || { echo "ERROR: photo-agent directory not found"; exit 1; }
python3 scripts/check_approval.py --callback-data reject 2>&1
```

## When invoked manually (e.g. /check_approval command)

Type `/check_approval` in Telegram after tapping the Approve button.
Do not ask the user any clarifying questions — always run this block immediately:

```bash
cd ~/src/fieldkit/platform/photo-agent || { echo "ERROR: photo-agent directory not found"; exit 1; }
python3 scripts/check_approval.py --callback-data approve 2>&1
```

## Output handling

Do not interpret the output yourself. Run the script once and do not retry.
If the exit code is non-zero, report it as an error: "Script failed (exit <code>): <output>"
If the script exits with code 0 and no output, report: "No pending approval."
Otherwise relay the output verbatim. Do not summarise or paraphrase.
