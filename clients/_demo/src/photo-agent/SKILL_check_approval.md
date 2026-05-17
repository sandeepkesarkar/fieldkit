---
name: check_approval
description: Check for a pending video approval response from the admin and process it
user-invocable: true
metadata: {"openclaw": {"requires": {"bins": ["python3"]}}}
---

# check_approval

Run the approval check script:

```bash
cd ~/src/fieldkit/clients/_demo/src/photo-agent || { echo "ERROR: photo-agent directory not found"; exit 1; }
python3 scripts/check_approval.py 2>&1
```

Do not interpret Telegram updates yourself. Run the script once and do not retry.
If the exit code is non-zero, report it as an error: "Script failed (exit <code>): <output>"
If the script exits with code 0 and no output, report: "No pending approval."
Otherwise relay the output verbatim. Do not summarise or paraphrase.
