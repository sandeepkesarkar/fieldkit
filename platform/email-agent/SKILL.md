---
name: check_email
description: Check Gmail inbox for new emails and send Telegram acknowledgements for each valid email received
user-invocable: true
metadata: {"openclaw": {"requires": {"bins": ["gws", "python3"], "env": ["AGENT_EMAIL", "ADMIN_ALLOWLIST", "ADMIN_TELEGRAM_CHAT_ID"]}}}
---

# check_email

Run the deterministic email intake script. Do not improvise or read emails yourself.

```bash
cd ~/src/fieldkit/platform/email-agent && python3 scripts/check_email.py
```

That is the entire procedure. The script handles everything: Gmail polling, allowlist enforcement, Telegram acknowledgements, stale alerts, and cycle logging. Report whatever output the script produces.
