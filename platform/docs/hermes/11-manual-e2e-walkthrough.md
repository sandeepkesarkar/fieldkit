# Manual End-to-End Walkthrough — Testing the Real Production Flow

Generic procedure for a human to manually exercise the full photo-agent
pipeline for **the currently-installed client** exactly as a real end user
would — real Telegram messages typed by a human, real files dragged into
Drive by hand, real `/process_photos`, `/photo_approve` / `/photo_reject`
commands. This is **not**
[`platform/photo-agent/scripts/run_e2e_test.py`](../../photo-agent/scripts/run_e2e_test.py)
and its `e2e_stage*.py` helpers — that rig *simulates* a user (uploads via
the Drive API directly, drives approval programmatically) to get fast,
reproducible CI-style coverage. This doc is for the times that isn't
enough: confirming the actual chat UX, actual Drive folder ergonomics, and
actual timing a human will experience, especially right after a change to
the approval flow (e.g. issue #49's button removal) where the thing being
verified *is* the human-facing surface itself.

Worked example throughout: `mercury`. Substitute the actually-installed
client's name, its `DRIVE_ROOT_FOLDER_ID`, and its Telegram bot everywhere
`mercury` appears.

> **Single-install model (issue #61):** this walkthrough assumes `mercury`
> is the client currently installed via
> `platform/photo-agent/scripts/install_client.sh mercury` — see
> [`09-per-client-model-profiles.md`](09-per-client-model-profiles.md). If
> a different client is installed, either switch first
> (`install_client.sh <that-client>`) or substitute that client's name
> throughout. There is exactly one Hermes profile (`default`) and one
> gateway (`ai.hermes.gateway`) involved — no `-p <profile>` flags anywhere
> in this doc.

**Working directory:** every command below is written relative to the
fieldkit repo root. Set it explicitly once, rather than assuming a fixed
clone location:
```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || echo ~/src/fieldkit)"
```

## 0. Pre-flight — confirm the install is actually ready for this

Do this before touching Drive or Telegram. All read-only.

1. **`mercury` is actually the installed client, not just the one you
   intend to test** — the single most important check under the
   single-install model, since a stale/forgotten install would otherwise
   have every step below silently run against the wrong client's Drive
   folder, Telegram bot, and Facebook Page:
   ```bash
   grep '^CLIENT_NAME=' .env
   grep -E '^(TELEGRAM_BOT_TOKEN|ANTHROPIC_API_KEY|OPENAI_API_KEY)=' ~/.hermes/.env | cut -d= -f1
   ```
   Expect `CLIENT_NAME=mercury`, and the second command to at least confirm
   a key is present for whichever provider `hermes config get model.provider`
   reports (see step 2). If `CLIENT_NAME` names a different client, run
   `platform/photo-agent/scripts/install_client.sh mercury` first — do not
   proceed on a mismatch and assume it'll sort itself out.

2. **Hermes's default profile is configured for the right model and
   skills:**
   ```bash
   hermes config get model.provider
   hermes config get model.default
   hermes skills list --source local
   ```
   Expect `photo-approve`, `photo-reject`, `process-photos` — all `local` /
   `enabled`. If you instead see `check-approval` (old name) or nothing at
   all, this can mean either (a) the gateway hasn't picked up the current
   skill files yet — restart it (`launchctl kickstart -k
   gui/$(id -u)/ai.hermes.gateway`) — or (b) `skills.external_dirs` isn't
   set on the default profile at all (`install_client.sh` sets it on every
   run, so this would mean the install script was never actually run, or
   ran against a different `HERMES_HOME`); check
   `hermes config get skills.external_dirs` before assuming a stale cache.
   See [`10-text-based-approval-migration.md`](10-text-based-approval-migration.md)
   for the full cutover this depends on.

3. **No approval already in flight** — a stray pending approval from a
   prior run would make a fresh `/process_photos` refuse to start
   (`process_photos.py` checks this itself and errors: *"already awaiting
   approval"*):
   ```bash
   jq .pending_approval clients/mercury/data/photo-agent/state.json
   ```
   Expect `null`. If not, resolve it first (`/photo_approve` or
   `/photo_reject` in the client's Telegram chat) before continuing.

4. **The gateway is actually running:**
   ```bash
   launchctl list | grep hermes
   ```
   Expect a line for `ai.hermes.gateway` (the bare default-profile label —
   there should be no `ai.hermes.gateway-<name>` entries unless a pre-#61
   profile hasn't been retired yet; see
   [`09-per-client-model-profiles.md`](09-per-client-model-profiles.md#what-happened-to-per-client-hermes-profiles)
   if so).

5. **`timeout`/`gtimeout` availability** — `process-photos/SKILL.md`
   itself now selects at runtime between GNU `timeout`, its Homebrew-
   installed alias `gtimeout`, or (if neither exists) no wrapper at all —
   this used to be an unconditional `timeout 660 python3 ...` that would
   fail outright with "command not found" on a machine lacking both, which
   is exactly what this Mac was confirmed to be missing (stock macOS ships
   neither binary). Check which case you're in before a live test:
   ```bash
   command -v timeout || command -v gtimeout || echo "NEITHER -- no enforced timeout on this machine"
   ```
   If it prints `NEITHER`, the pipeline still runs correctly — you simply
   lose the skill's own 11-minute hard cap (the pipeline itself remains
   bounded by Drive/network timeouts, just not by this skill's own
   deadline). `brew install coreutils` (provides `gtimeout`) restores the
   hard cap. This is a real gap in this machine's environment, independent
   of this doc — fixed at the SKILL.md level so the doc's claim and the
   script's actual behavior can't drift apart again the way they did
   before.

6. **Cron legs this test doesn't touch are — at minimum — not visibly
   broken.** `upload_facebook.py` still runs on cron (unaffected by the
   approval-flow migration):
   ```bash
   tail -20 logs/cron.log | grep upload_facebook
   ```
   **This is a weaker check than it looks.** `upload_facebook.py` only
   logs at `WARNING`+ (same posture as `process_photos.py` — see step 2 of
   §2 below), so a quiet-looking tail here does not positively confirm the
   cron leg is healthy; it only rules out it having logged a warning or
   error recently. Absence of output is inconclusive, not proof.

7. **Known leftover to be aware of, not to fix here:** as of this writing,
   `clients/mercury/src/photo-agent/.env` still has a
   `TELEGRAM_APPROVAL_BOT_TOKEN` line left over from the pre-#49 dual-bot
   flow. It is dead — nothing in the codebase reads it anymore (confirmed
   by grep) — so it does not affect this walkthrough. It's flagged in
   [`10-text-based-approval-migration.md`](10-text-based-approval-migration.md)'s
   cutover checklist (step 3.2) as cleanup still owed for `mercury`; don't
   do that surgery as part of a test run — file or perform it separately.

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
`fieldkit_mercury_bot`). If you're unsure this is actually the bot bound to
`TELEGRAM_BOT_TOKEN` in `clients/mercury/src/photo-agent/.env`, don't try to
visually match the token to the username — a bot token isn't something you
can eyeball against a `@username`. Ask Telegram directly instead — **not**
by substituting the token into a `curl` URL argument (that puts it in
`ps`/process-listing output for the command's whole runtime); pipe a `curl`
config line containing the token via stdin instead, so the token never
appears in any process's argv:
```bash
grep '^TELEGRAM_BOT_TOKEN=' clients/mercury/src/photo-agent/.env | cut -d= -f2- \
  | awk '{print "url = \"https://api.telegram.org/bot" $0 "/getMe\""}' \
  | curl -s -K -
```
The `"username"` field in the response is the authoritative answer — compare
that to the chat you're about to type into.

Type, as a normal message:
```
/process_photos manual-e2e-20260826
```
(replace with whatever folder name you created in step 1)

**What happens next, and how you'll know it's working:**

- Hermes's gateway picks this up on its normal poll — no separate step to
  "start" anything.
- The script runs **synchronously**, for up to 11 minutes IF this machine has
  `timeout` or `gtimeout` on `PATH` — otherwise there's no enforced cap at
  all (see step 5 of the pre-flight section above; `process-photos/SKILL.md`
  now selects the wrapper it uses at runtime rather than assuming one
  exists) — Drive folder lookup, photo count/name validation, download,
  ffmpeg video generation, Drive upload of the result. For 2–5 photos at
  `SECONDS_PER_PHOTO=4` (mercury's configured value) this is normally well
  under a minute; do not assume it's stuck just because Telegram shows
  nothing yet.
- **Important, verified behavior:** `process_photos.py` contains no
  `print()` calls at all, on any path — confirmed by reading the script
  directly. All its own output goes through Python's `logging` module,
  configured at `WARNING` level to `stderr`
  (`logging.basicConfig(level=logging.WARNING, stream=sys.stderr)`), which
  the `2>&1` at the end of `process-photos/SKILL.md`'s invocation merges into
  the single stream Hermes actually captures. On a clean successful run
  nothing hits `WARNING`, so that merged stream is empty and Hermes's own
  relay of the command's output will typically show as empty. Don't wait
  for a text confirmation from Hermes itself. The real signal that it
  worked is a **separate** Telegram message, sent directly by the script
  (not by Hermes relaying anything), that reads:
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
  filename, etc.) works the **same way as the success path above, not
  differently**: `process_photos.py`'s own `_telegram_error()` helper sends
  the `❌ ...` message directly via the Telegram Bot API and then exits — it
  is not printed to stdout/stderr and not something Hermes relays. Confirmed
  by reading the script: every validation failure calls this same helper.
  Hermes's own separate relay of the command's exit code and captured
  output (per `process-photos/SKILL.md`'s "report it as an error"
  instruction) will typically still show as empty or near-empty for the
  same reason as the success case above — the `❌ ...` text you see in
  Telegram is the script's own direct message, not that relay.

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
- **Facebook post** — grab the post id from the client's own state, matched
  to the **specific project you just ran**, not the last entry in
  `published_history` — this is a real Page with real traffic, and the last
  entry could just as easily be a real client publish that happened to land
  right before or after your test run, not this test's post. Fail closed
  rather than guess if the match isn't exactly one:
  ```bash
  PROJECT="manual-e2e-20260826"   # the project name from step 1
  MATCHES=$(jq --arg p "$PROJECT" \
    '[.published_history[] | select(.project_name == $p)]' \
    clients/mercury/data/photo-agent/facebook_state.json)
  COUNT=$(echo "$MATCHES" | jq 'length')
  if [ "$COUNT" != "1" ]; then
    echo "REFUSING: expected exactly 1 published_history entry for project=$PROJECT, found $COUNT — resolve manually before proceeding" >&2
  else
    FB_POST_ID=$(echo "$MATCHES" | jq -r '.[0].fb_post_id')
    echo "fb_post_id=$FB_POST_ID"
  fi
  ```
  Open `https://www.facebook.com/$FB_POST_ID` in a browser and confirm the
  video is actually there and public. Keep this shell session open — step
  5 below reuses `$FB_POST_ID` (re-run this snippet first if you're
  starting a new shell).

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
   do, from a shell using the same client credentials. Reuse `$FB_POST_ID`
   resolved in step 4 above (the fail-closed, project-matched lookup) — do
   **not** hand-copy a post id from memory or from the Page's UI feed, where
   it's easy to click the wrong post on a Page with real traffic:
   Validate the post id looks like a real Facebook post id (`<page_id>_<post_id>`,
   digits and one underscore) before using it for anything — a malformed
   value here is a sign the lookup in step 4 went wrong, not something to
   pass through regardless:
   ```bash
   [[ "$FB_POST_ID" =~ ^[0-9]+_[0-9]+$ ]] || { echo "REFUSING: FB_POST_ID doesn't look like a real post id: $FB_POST_ID" >&2; }
   ```
   Then delete it, passing the id through an environment variable rather
   than interpolating it into the Python source directly:
   ```bash
   cd platform/photo-agent
   CLIENT_NAME=mercury FB_POST_ID_TO_DELETE="$FB_POST_ID" python3 -c "
   import os
   from dotenv import load_dotenv
   load_dotenv('../../.env'); load_dotenv('../../clients/mercury/src/photo-agent/.env', override=True)
   from tools import facebook_api
   facebook_api.delete_post(os.environ['FB_PAGE_ACCESS_TOKEN'], os.environ['FB_POST_ID_TO_DELETE'])
   print('deleted')
   "
   ```
2. **Re-fetch to prove it's actually gone** — re-visit
   `https://www.facebook.com/$FB_POST_ID` and confirm it now 404s / shows
   "content isn't available", or re-run the same `delete_post` call and
   confirm it now raises a `FacebookUploadError` for Graph API error code
   `100` ("post not found") — that specific code is the documented signal a
   post no longer exists, not a fresh failure.

## 6. Differences from the automated e2e rig — read if something looks "wrong"

- The rig (`run_e2e_test.py`) uploads synthetic frames via the Drive API and
  drives approval programmatically — it never touches Telegram or a
  browser. If you're used to reading its output, expect this walkthrough to
  *feel* much slower and much quieter — that's real Telegram/Hermes
  latency, not a bug.
- The rig can target a non-installed client via the inline `CLIENT_NAME=`
  override (see
  [`09-per-client-model-profiles.md`](09-per-client-model-profiles.md#what-if-i-need-to-test-a-non-active-client-without-switching)).
  This walkthrough cannot — live Telegram/Hermes skill dispatch always
  operates on whatever client is currently installed, which is the whole
  point of step 0.1 above.
