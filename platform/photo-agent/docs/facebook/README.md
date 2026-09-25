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

---

## Lost upload responses and duplicate posts (issue #78)

`upload_facebook.py` uploads through Meta's **sessionized** video upload on the Page
`/videos` edge (`upload_phase` = `start` → `transfer` → `finish`, against
`graph-video.facebook.com`). The `start` phase returns a `video_id` before any bytes
move, and nothing is published until `finish`. FieldKit writes that `video_id` to
`facebook_state.json` straight after `start`, and writes a `publish_attempted_at`
marker **before** sending `finish`.

So if `finish` succeeds at Meta but its response is lost (crash, kill, timeout,
network partition), the job still names the exact video to ask about:

- The next attempt reads `GET /{video_id}?fields=status` **before uploading
  anything**. `publishing_phase.publish_status = published` → recorded as published
  (`FB_RECOVER`), nothing re-posted. `video_status` `error` / `upload_failed` /
  `expired`, or `publish_status = error` → it did not publish, so a fresh upload runs.
  Anything else (still processing, draft, no answer) → the attempt fails **without
  uploading**.
- If the job runs out of attempts with the answer still unknown, the video is
  quarantined in `pending_publish_reconciliations` (`FB_UNKNOWN`). That blocks
  re-approval of the same video, and the admin gets an alert naming the video id.
  Every tick re-checks it. The block lifts only when Facebook answers definitively
  about **that video id**.

FieldKit never decides from the Page's list of recent videos. A time match can't
tell FieldKit's upload apart from one a person posted, so it isn't treated as an answer.

### Resolving a quarantine by hand

Normally you do nothing: the drain resolves entries once Facebook answers. If one is
stuck (for example, Facebook never reports a `publish_status` for that video):

1. Read `pending_publish_reconciliations` in `facebook_state.json`. Each entry names
   `project_name`, `video_id` and `recorded_at`.
2. Open `https://www.facebook.com/<video_id>` and the Page itself.
3. If the video IS live, remove the entry and add its `idempotency_key` to
   `published_idempotency_keys`, so it cannot be re-approved into a second post.
4. If it is NOT live, remove the entry. The video can then be re-approved normally.

Edit `facebook_state.json` only while no cron tick is running. Removing an entry you
have not actually checked is the one way back to a duplicate post.
