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

Normally you do nothing: the drain clears entries by itself once Facebook answers. For an
entry that stays stuck (for example, Facebook never reports a `publish_status` for that
video), use the operator tool. **Don't** edit `facebook_state.json` by hand, and **don't**
release a key because the video "isn't on the Page". An accepted publish can still be
processing, so not seeing it now doesn't mean it will never go live.

```
cd platform/photo-agent
python3 scripts/resolve_facebook_quarantine.py list
python3 scripts/resolve_facebook_quarantine.py resolve <video_id>
```

`resolve` settles the outcome instead of guessing it:

1. It reads `GET /{video_id}?fields=status`. If Facebook reports the video **published**,
   the tool records it as published, which retires the key permanently. It deletes nothing
   and exits 0. A live video is recorded, never released.
2. Otherwise it sends `DELETE /{video_id}` for **that exact video**, then reads the node
   back and requires Graph error `100` (object does not exist). Only then does it release
   the key and exit 0. The one video that might have gone live no longer exists, so
   re-approving leaves one post, not two.
3. If the delete fails, the quarantine stays and the tool exits 1. It also stays if the
   delete says the video **does not exist** (code 100 on the DELETE itself), or if the
   video can still be read afterwards. A "does not exist" error isn't proof the video never
   went live, because Graph gives the same error for a video that's gone and for one this
   token can't see. Check the Page and the token's permissions, then run `resolve` again.

On the Meta side, the Page `/videos` reference sends deletes to the Video node, and Meta's
generated SDK (`facebook_business/adobjects/advideo.py`, `api_delete`) issues
`DELETE /{video_id}`. No Meta documentation was found saying that a deleted video
can't still finish an in-flight publish. That's why step 2 requires the follow-up read
before it releases anything.

If `resolve` still can't settle an entry and you decide to release the key anyway:

```
python3 scripts/resolve_facebook_quarantine.py override <video_id> --accept-duplicate-risk
```

This releases the key **without a definitive answer**. If the video was in fact published,
re-approving will post it a second time. The flag is required so that choice is made
knowingly, and the tool logs it as `FB_RESOLVED ... observed=operator_override_accepts_duplicate_risk`.

The tool holds `upload_facebook.lock`, so it won't run while a cron tick is mid-flight. It
sends the Page token only in the Authorization header and redacts every error it prints.
