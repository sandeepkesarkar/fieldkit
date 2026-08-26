# Manual End-to-End Walkthrough — Testing the Real Production Flow (worked example: `mercury`)

Generic procedure for a human to manually exercise the full photo-agent
pipeline for **any live client** exactly as a real end user would — real
Telegram messages typed by a human, real files dragged into Drive by hand,
real `/process_photos`, `/photo_approve` / `/photo_reject` commands. This is
**not** [`platform/photo-agent/scripts/run_e2e_test.py`](../../photo-agent/scripts/run_e2e_test.py)
and its `e2e_stage*.py` helpers — that rig *simulates* a user (uploads via
the Drive API directly, drives approval programmatically) to get fast,
reproducible CI-style coverage. This doc is for the times that isn't enough:
confirming the actual chat UX, actual Drive folder ergonomics, and actual
timing a human will experience, especially right after a change to the
approval flow (e.g. issue #49's button removal) where the thing being
verified *is* the human-facing surface itself.

Worked example throughout: `mercury`. Substitute the client name, its
`DRIVE_ROOT_FOLDER_ID`, and its Telegram bot everywhere `mercury` appears to
reuse this for `venus` or any future client.

## 0. Pre-flight — confirm the client is actually ready for this

Do this before touching Drive or Telegram. All read-only.

1. **No approval already in flight** — a stray pending approval from a prior
   run would make a fresh `/process_photos` refuse to start (`process_photos.py`
   checks this itself and errors: *"already awaiting approval"*):
   ```bash
   jq .pending_approval clients/mercury/data/photo-agent/state.json
   ```
   Expect `null`. If not, resolve it first (`/photo_approve` or
   `/photo_reject` in the client's Telegram chat) before continuing.

2. **New-flow skills are registered under the client's Hermes profile:**
   ```bash
   hermes -p mercury skills list --source local
   ```
   Expect `photo-approve`, `photo-reject`, `process-photos` — all `local` /
   `enabled`. If you instead see `check-approval` (old name) or nothing at
   all, the gateway hasn't picked up the current skill files — restart it
   (`launchctl kickstart -k gui/501/ai.hermes.gateway-mercury`, or the bare
   `ai.hermes.gateway` label for a client on the default profile) before
   continuing. See
   [`10-text-based-approval-migration.md`](10-text-based-approval-migration.md)
   for the full cutover this depends on.

3. **The client's gateway is actually running:**
   ```bash
   launchctl list | grep hermes
   ```
   Expect a line for this client's label (`ai.hermes.gateway-mercury` for a
   named profile).

4. **Cron legs this test doesn't touch are healthy** — `upload_facebook.py`
   still runs on cron (unaffected by the approval-flow migration) and should
   show clean recent ticks, not a repeating error:
   ```bash
   tail -20 logs/cron.log | grep upload_facebook
   ```

5. **Known leftover to be aware of, not to fix here:** as of this writing,
   `clients/mercury/src/photo-agent/.env` still has a `TELEGRAM_APPROVAL_BOT_TOKEN`
   line left over from the pre-#49 dual-bot flow. It is dead — nothing in
   the codebase reads it anymore (confirmed by grep) — so it does not affect
   this walkthrough. It's flagged in
   [`10-text-based-approval-migration.md`](10-text-based-approval-migration.md)'s
   cutover checklist (step 3.2) as cleanup still owed for `mercury`; don't do
   that surgery as part of a test run — file or perform it separately.

## 1. Add photos to the client's Drive intake folder — by hand, in a browser

Open the client's Drive **root** folder directly:

```
https://drive.google.com/drive/folders/<DRIVE_ROOT_FOLDER_ID>
```

For `mercury` (from `clients/mercury/src/photo-agent/.env`):
```
https://drive.google.com/drive/folders/1ferhXoeFCjvQLa_zOgklM-OTn2mCXFEm
```

Inside that root folder:

1. **Create a new subfolder.** Its name **is** the project name you'll pass
   to `/process_photos` later — the pipeline finds it by exact name match
   directly under the root (`drive.find_folder(project_name, root_folder_id)`),
   one level deep only. Name must match `^[A-Za-z0-9_-]+$` (letters, digits,
   `_`, `-` — no spaces). Use something obviously a manual test and
   timestamped so it can't collide with a real client project or a prior
   e2e-rig run, e.g. `manual-e2e-20260826`.
2. **Drag in between 2 and 30 photo files**, `.jpg`/`.jpeg` or `.png` only
   (`image/jpeg` / `image/png` MIME types — the pipeline ignores anything
   else in the folder, and a `.mp4` does **not** belong here: the pipeline
   *generates* the video itself from these photos via ffmpeg, it doesn't
   expect one uploaded). No two files may share the same sanitized filename,
   and no zero-byte files.
3. That's it — nothing needs to be triggered from the Drive side. There is
   **no folder-watcher**; Drive is purely where the pipeline looks when a
   human later tells it to, via the Telegram command below.

## 2. Trigger processing — the real command, in the real chat

Open a Telegram chat with the client's bot (for `mercury`,
`fieldkit_mercury_bot` — confirm this matches `TELEGRAM_BOT_TOKEN` in
`clients/mercury/src/photo-agent/.env` if you're unsure which bot that is).

Type, as a normal message:
```
/process_photos manual-e2e-20260826
```
(replace with whatever folder name you created in step 1)

**What happens next, and how you'll know it's working:**

- Hermes's gateway picks this up on its normal poll — no separate step to
  "start" anything.
- The script runs **synchronously**, for up to 11 minutes (the skill enforces
  a 660s timeout) — Drive folder lookup, photo count/name validation,
  download, ffmpeg video generation, Drive upload of the result. For 2–5
  photos at `SECONDS_PER_PHOTO=4` (mercury's configured value) this is
  normally well under a minute; do not assume it's stuck just because
  Telegram shows nothing yet.
- **Important, verified behavior:** on a clean successful run,
  `process_photos.py` prints **nothing to stdout** and only logs at
  `WARNING`+ to stderr — so Hermes's own relay of the command's output will
  typically show as empty. Don't wait for a text confirmation from Hermes
  itself. The real signal that it worked is a **separate** Telegram message,
  sent directly by the script (not by Hermes relaying anything), that reads:
  ```
  📸 manual-e2e-20260826 — <N> photos, <duration>s video
  View folder: https://drive.google.com/drive/folders/<folder_id>
  Reply /photo_approve or /photo_reject.
  ```
  with **no buttons** — if you see inline buttons, the client is still on
  the old flow and step 0.2 above needs revisiting.
- For a live, human-readable blow-by-blow while you wait, tail the client's
  own log instead of watching Telegram:
  ```bash
  tail -f clients/mercury/logs/photo-agent.log
  ```
  Expect lines like `DOWNLOADED`, `GENERATED`, `UPLOADED`, `APPROVAL_REQ` to
  appear in order as the run progresses.
- Any validation failure (wrong photo count, folder not found, duplicate
  filename, etc.) comes back as a Hermes-relayed `❌ ...` error message —
  those *do* produce stdout, so they show up normally.

## 3. Respond to the approval message

In the **same** Telegram chat, reply with exactly one of:
```
/photo_approve
```
or
```
/photo_reject
```
- Only one approval can be pending per client at a time — the command needs
  no project name or other argument.
- Expect a plain-text relayed result (email sent / Facebook upload enqueued,
  or Drive+temp-file cleanup on reject) — again, no buttons.
- Confirm the state cleared:
  ```bash
  jq .pending_approval clients/mercury/data/photo-agent/state.json
  ```
  Expect `null` again.
- Confirm the log shows the full chain:
  ```bash
  tail -10 clients/mercury/logs/photo-agent.log
  ```
  On approve, expect `APPROVED` followed shortly by `FB_STARTED` and
  `FB_PUBLISHED` (Facebook upload runs on its own cron leg, `* * * * *` —
  allow up to a minute after approving before `FB_PUBLISHED` appears).

## 4. Verify the downstream effects landed for real

- **Approval email** — confirm `ADMIN_EMAIL` (from the client's `.env`)
  actually received it.
- **Facebook post** — grab the post id from the client's own state, don't
  guess it:
  ```bash
  jq '.published_history[-1]' clients/mercury/data/photo-agent/facebook_state.json
  ```
  Open `https://www.facebook.com/<fb_post_id>` in a browser and confirm the
  video is actually there and public.

## 5. Clean up afterward — with re-fetch verification, not just "I clicked delete"

Don't leave test artifacts sitting in a live client's Drive or Facebook Page.

**Drive:**
1. Open the project subfolder from step 1 and delete it (trash it) in the
   Drive UI.
2. Re-open the root folder URL from step 1 and confirm the subfolder is gone
   from the listing (or check Drive's Trash to confirm it landed there).

**Facebook:**
1. Delete the post — either through the Facebook Page's own UI (Page → Posts
   → find it → delete), or, to match exactly what the pipeline itself would
   do, from a shell using the same client credentials:
   ```bash
   cd platform/photo-agent
   CLIENT_NAME=mercury python3 -c "
   import os
   from dotenv import load_dotenv
   load_dotenv('../../.env'); load_dotenv('../../clients/mercury/src/photo-agent/.env', override=True)
   from tools import facebook_api
   facebook_api.delete_post(os.environ['FB_PAGE_ACCESS_TOKEN'], '<fb_post_id>')
   print('deleted')
   "
   ```
2. **Re-fetch to prove it's actually gone** — re-visit
   `https://www.facebook.com/<fb_post_id>` and confirm it now 404s / shows
   "content isn't available", or re-run the same `delete_post` call and
   confirm it now raises a `FacebookUploadError` for Graph API error code
   `100` ("post not found") — that specific code is the documented signal a
   post no longer exists, not a fresh failure.

## 6. Differences from the automated e2e rig — read if something looks "wrong"

- The rig (`run_e2e_test.py`) uploads synthetic frames via the Drive API and
  drives approval programmatically — it never touches Telegram or a
  browser. If you're used to reading its output, expect this walkthrough to
  *feel* much slower and much quieter — that's real Telegram/Hermes latency
  and the empty-stdout behavior above, not a bug.
- The rig's own runs leave real, dated entries in
  `facebook_state.json` / `photo-agent.log` (e.g. `e2e-test-<timestamp>`
  project names) — don't mistake those for artifacts of *your* manual run
  when reading state files; match on your own project name.

## Mercury-specific status snapshot (as of 2026-08-26 — re-verify before relying on this)

- Cron: no `check_approval.py` entry — old poller already removed.
- Skills: `photo-approve` / `photo-reject` / `process-photos` registered
  under the `mercury` Hermes profile; `check-approval` absent.
- `TELEGRAM_BOT_TOKEN` in `clients/mercury/src/photo-agent/.env` hash-matches
  `~/.hermes/profiles/mercury/.env`'s token — single-bot consolidation
  prerequisite holds.
- `pending_approval` is `null` — clear to run a test immediately.
- **Open cleanup item, not done as part of this doc:** delete the unused
  `TELEGRAM_APPROVAL_BOT_TOKEN` line from `clients/mercury/src/photo-agent/.env`
  per [`10-text-based-approval-migration.md`](10-text-based-approval-migration.md)
  step 3.2, and retire the old approval bot registration via BotFather (step
  7) once a rollback window has passed.
