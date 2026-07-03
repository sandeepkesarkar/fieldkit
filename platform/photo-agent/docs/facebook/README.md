# Facebook Integration — Setup Docs

Step-by-step guides for connecting FieldKit to a Facebook Page.
Work through them in order the first time; individual docs can be re-used
when reconnecting or debugging.

---

## Guides

| # | File | When to use |
|---|------|-------------|
| 1 | [Create the Meta Developer App](01-create-app.md) | Once per FieldKit installation |
| 2 | [Get tokens and test manually](02-manual-test.md) | After app creation; verify everything works before running the script |
| 3 | [`generate_auth_link.py` reference](../../SKILL_generate_auth_link.md) | Automated token setup for production use |
| 4 | [`upload_facebook.py` reference](../../SKILL_upload_facebook.md) | Cron upload setup |

---

## Prerequisites (for all guides)

- A Facebook account with admin access to the Page you want to post to
- Node: the account that creates the developer app and the account that owns the Page **can be the same personal account**
- Python 3.11+ and FieldKit installed locally

---

## Quick orientation: token types

Facebook authentication uses three token types in sequence:

```
Short-lived user token  (1–2 hours)   ← OAuth code exchange
        ↓  exchange via /oauth/access_token
Long-lived user token   (~60 days)    ← store, use for page token refresh
        ↓  GET /{user_id}/accounts
Page access token       (never expires) ← what FieldKit uses at runtime
```

`generate_auth_link.py` performs all three steps automatically.
The manual test guide (doc 2) walks through each step individually so you can verify the chain works before automation.
