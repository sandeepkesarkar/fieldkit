# 002 — Photo Video Agent: Clarifications

Resolved ambiguities from `spec.md`. These decisions are binding for technical planning.

---

## Drive API Access

Google Drive operations use `gws` (the same Google Workspace CLI already installed on the Mac Mini for Gmail). All Drive calls go through `gws drive ...` subcommands — no separate Python SDK or OAuth credentials.

**Why:** Keeps the toolchain consistent with Feature 001. gws already has an authenticated session on the Mac Mini. Adding Drive operations to an existing gws session is one re-auth step, not a new dependency.

---

## OAuth Scopes — Re-Auth Required

The existing gws session was authorised for Gmail scopes only. Drive access requires adding `https://www.googleapis.com/auth/drive` to the scope set. This means a one-time interactive re-auth on the Mac Mini before the feature can run.

Re-auth steps are documented in `SETUP.md`. The re-auth does not affect the Gmail session — gws merges scopes.

**Why:** OAuth scopes are additive; the Gmail token cannot be reused for Drive. A one-time manual step is acceptable — it only ever happens once per Mac Mini deployment.

---

## Telegram Approval Callback — Two-Script Architecture

Handling inline keyboard callbacks requires reading Telegram `callback_query` updates. The trigger script (`process_photos.py`) must not block waiting for the admin to respond — that could be minutes or days.

**Decision:** Two separate scripts, both driven by cron or on-demand triggers:

1. **`process_photos.py`** — triggered by `/process_photos <name>` via OpenClaw skill; runs end-to-end (download → scrub → generate → upload → send approval message); writes a `PENDING_APPROVAL` record to state and **exits**.

2. **`check_approval.py`** — run by a 1-minute cron job; calls `gws telegram updates list` (or equivalent) to fetch new Telegram updates; filters for `callback_query` events that match a known pending approval; dispatches approve or reject logic; exits.

This mirrors the email agent pattern (cron fires a short-lived script) and keeps both scripts independently testable.

**Why:** A blocking script waiting for a callback is operationally fragile — it occupies a process indefinitely and cannot survive Mac Mini reboots. The cron+state pattern is already proven in Feature 001.

---

## Pending Approval State Persistence

When `process_photos.py` finishes and sends the approval request, it writes a record to `data/photo-agent/state.json`:

```json
{
  "pending_approval": {
    "project_name": "kitchen_remodel",
    "drive_folder_id": "1abc...",
    "drive_video_file_id": "1xyz...",
    "telegram_message_id": 42,
    "triggered_at": "2026-05-12T14:32:00Z"
  }
}
```

`check_approval.py` reads this record. On approval or rejection it clears `pending_approval` (sets to `null`).

**Single active approval at a time:** If admin sends `/process_photos` while a `PENDING_APPROVAL` record already exists, the agent replies:

```
⚠ A video for '<project_name>' is already awaiting approval.
Tap ✅ Approve or ❌ Reject in the earlier message before starting a new one.
```

**Why:** One pending approval at a time is the simplest correct model. Allowing multiple simultaneous approvals would require matching each callback to its originating project — complex state, no practical benefit for a single-admin setup.

**Agent restart recovery:** If the Mac Mini restarts while `pending_approval` is set, the next `check_approval.py` cron run picks up the record and resumes polling. The admin's inline keyboard buttons remain active in Telegram — the callback is not lost on restart.

---

## Concurrent Processing Guard

`process_photos.py` acquires an exclusive lock file (`data/photo-agent/run.lock`) using `fcntl.flock` before doing any work, and releases it on exit. This prevents a second cron-triggered or manually triggered run from overlapping.

Same pattern as Feature 001's email agent lock.

**Why:** A user triggering `/process_photos` while the previous run is still generating the video would corrupt the temp directory and produce a broken upload.

---

## Drive Folder Link Format

The Drive folder link is constructed from the folder ID:

```
https://drive.google.com/drive/folders/<drive_folder_id>
```

No public share is created. The admin is the owner of the Drive folder and can access any Drive link directly. The link is used in both the Telegram approval message and the approval email.

**Why:** Creating a public share would expose project photos to anyone with the link. The admin already has owner access — constructing the link from the folder ID is sufficient and keeps photos private.

---

## Photo Input Validation Rules

| Condition | Behaviour |
|-----------|-----------|
| Fewer than 2 supported image files in folder | Abort; Telegram error |
| More than 30 image files in folder | Abort; Telegram error: "Too many photos (max 30). Remove extras and try again." |
| Unsupported file type (e.g. `.heic`, `.pdf`) | Silently skipped; not counted toward the minimum |
| Zero-byte file | Skipped with a warning logged; not counted toward minimum |

The 30-photo limit exists to keep video duration reasonable and Drive upload size predictable.

**Why a hard limit rather than truncation:** Silently dropping photos would surprise the admin. An error prompts them to curate the folder intentionally.

---

## Photo Scaling Strategy

Each photo is scaled to fill 1080 × 1920 (portrait) using **scale-then-center-crop**. No letterboxing (black bars). Landscape photos will be cropped on the sides; portrait photos will be cropped top and bottom if their aspect ratio does not match 9:16 exactly.

FFmpeg filter chain per photo:

```
scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920
```

**Why no letterbox:** Black bars look unprofessional on mobile social feeds. Cropping is the industry convention for short-form video content (Instagram Reels, TikTok). The admin controls which photos are included and how they are framed — that is sufficient control.

---

## FFmpeg Crossfade Duration

Crossfade transition between each photo is **0.5 seconds**.

Total video duration formula:

```
duration = (N × seconds_per_photo) - ((N - 1) × 0.5)
```

Example: 5 photos × 4 sec − 4 × 0.5 sec = 18 seconds.

**Why 0.5 sec:** Short enough that it does not noticeably shorten content time; long enough to look smooth on mobile. 1 second felt slow in testing; 0.25 second felt abrupt.

---

## Email Delivery

The approval email is sent via `gws gmail users messages send` — identical to how Feature 001 sends the stale alert email. The sender address is the agent Gmail account (`agent_email` from `.env`).

**Why:** Same mechanism already proven in Feature 001. No new dependency.

---

## Temp File Lifecycle

| Event | Local temp file action |
|-------|----------------------|
| Video generated | File created at `video_tmp_dir/<project_name>/<filename>.mp4` |
| Drive upload succeeds | File retained until approval decision |
| Admin approves | File deleted |
| Admin rejects | File deleted |
| Drive upload fails | File **retained** — logged with path for manual recovery |
| Any earlier phase fails | Partial downloads deleted; no video file created |

The `video_tmp_dir/<project_name>/` directory is deleted and recreated at the start of each new run for that project, ensuring no stale downloads from a prior failed run persist.

**Why retain on upload failure:** The file took compute time and network round-trips to produce. Retaining it allows manual upload or investigation without re-running the full pipeline.

---

## Telegram Update Polling — Offset Tracking

`check_approval.py` tracks the Telegram update offset in `data/photo-agent/state.json` alongside the pending approval record:

```json
{
  "telegram_update_offset": 157,
  "pending_approval": { ... }
}
```

Each run of `check_approval.py` passes the stored offset to `getUpdates`, processes all returned updates, and writes `max(update_id) + 1` back as the new offset. This prevents replaying the same callback_query across cron runs.

**Why:** Without offset tracking, the first cron run after an approval processes the callback correctly, but the second run sees the same update again and attempts to approve or reject a project that no longer has a pending record. The offset eliminates the replay.

---

## `/check_approval` as a Separate On-Demand Telegram Command

In addition to the 1-minute cron, the admin can send `/check_approval` via Telegram to trigger an immediate approval-check cycle outside the cron schedule. This is the approval-side equivalent of `/check_email`.

**Why:** If the cron is delayed or the admin wants immediate feedback after tapping Approve/Reject, the on-demand command provides a manual escape hatch consistent with the Feature 001 pattern.
