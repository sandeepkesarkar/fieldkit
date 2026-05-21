# 002 — Photo Video Agent: Technical Plan

**Status:** Technical Planning
**Spec:** [`spec.md`](spec.md)
**Clarifications:** [`clarify.md`](clarify.md)
**Last Updated:** 2026-05-12

---

## Stack

| Concern | Solution | Rationale |
|---------|----------|-----------|
| Runtime & orchestration | OpenClaw | Handles skill dispatch, Telegram channel, cron |
| Drive operations (listing, download, upload) | `gws` CLI | Already installed; supports `--output` for binary downloads and `+upload` for multipart uploads |
| Telegram send (text only, errors, confirmations) | OpenClaw Telegram channel (`openclaw message send`) | Consistent with Feature 001 |
| Telegram send with inline keyboard | Direct Telegram Bot API (`requests`) | OpenClaw `message send` does not support `reply_markup` |
| Telegram callback polling (`getUpdates`) | Direct Telegram Bot API (`requests`) | OpenClaw has no CLI command for reading incoming updates or callbacks |
| Video generation | FFmpeg via `subprocess` behind `VideoGenerator` protocol | On-premise, free, no external API, swappable |
| Privacy scrubbing | No-op `scrub()` in pipeline | Placeholder; activatable without restructuring |
| State persistence | `tools/state.py` + `data/photo-agent/state.json` | Consistent with Feature 001 pattern |
| Local logging | Append-only `logs/photo-agent.log` | Consistent with Feature 001 pattern |
| Testing | `pytest` + `pytest-mock` | Standard across the framework |

---

## Architecture: Two-Script Pattern

`process_photos.py` runs on demand and exits after sending the approval request. `check_approval.py` runs on a 1-minute cron and polls for the admin's Telegram callback. Neither script blocks.

```
Admin sends /process_photos kitchen_remodel
         │
         ▼
 process_photos.py
   ├─ Looks up Drive folder
   ├─ Downloads photos
   ├─ Scrubs (no-op)
   ├─ Generates video (FFmpeg)
   ├─ Uploads video to Drive
   ├─ Sends Telegram message with ✅/❌ buttons
   ├─ Writes PENDING_APPROVAL to state.json
   └─ Exits

check_approval.py  (runs every 1 min via cron)
   ├─ Reads state.json — pending_approval present?
   │     No → exit
   │     Yes ↓
   ├─ Calls Telegram getUpdates with stored offset
   ├─ Finds callback_query matching pending message_id?
   │     No → update offset, exit
   │     Yes (✅ Approve) → send email + Telegram confirm + cleanup
   │     Yes (❌ Reject)  → delete Drive video + cleanup + Telegram notify
   └─ Clears pending_approval, updates offset, exits
```

---

## Components

### 1. `/process_photos` Skill (`SKILL_process_photos.md`)

User-invocable OpenClaw skill. Admin sends `/process_photos kitchen_remodel` → OpenClaw LLM extracts the project name and passes it as `--project`:

```markdown
---
name: process_photos
description: Generate a video from photos in a Google Drive project folder and send it to the admin for approval
user-invocable: true
metadata: {"openclaw": {"requires": {"bins": ["gws", "python3", "ffmpeg"]}}}
---

# process_photos

The admin provides a project name after the command (e.g. `/process_photos kitchen_remodel`).

Extract the project name — everything after `/process_photos`, trimmed of whitespace.

If no project name was provided, reply:
"Please provide a project name — e.g. /process_photos kitchen_remodel"

Otherwise run:

```bash
cd ~/src/fieldkit/clients/_demo/src/photo-agent && \
  python3 scripts/process_photos.py --project <extracted_project_name>
```

Do not access Drive or generate the video yourself. Report whatever the script prints.
```

### 2. `/check_approval` Skill (`SKILL_check_approval.md`)

On-demand escape hatch. Admin sends `/check_approval` to trigger an immediate approval-check cycle:

```markdown
---
name: check_approval
description: Check for a pending video approval response from the admin and process it
user-invocable: true
metadata: {"openclaw": {"requires": {"bins": ["python3"]}}}
---

# check_approval

Run the approval check script:

```bash
cd ~/src/fieldkit/clients/_demo/src/photo-agent && \
  python3 scripts/check_approval.py
```

Do not interpret Telegram updates yourself. Report whatever the script prints.
```

### 3. Approval Cron Job

A 1-minute system cron runs `check_approval.py` automatically. Registered once during setup using the heredoc pattern (same as Feature 001):

```bash
(crontab -l 2>/dev/null; cat <<'EOF'
* * * * * env PATH=/usr/local/bin:/usr/bin:/bin PYTHON3=$(which python3) \
  $PYTHON3 ~/src/fieldkit/clients/_demo/src/photo-agent/scripts/check_approval.py \
  --source cron >> ~/src/fieldkit/logs/cron.log 2>&1
EOF
) | crontab -
```

### 4. Drive Wrapper (`tools/drive.py`)

Thin wrapper around `gws drive` subcommands. All output is JSON; file transfers use `--output` and `+upload` flags.

| Operation | gws Command |
|-----------|-------------|
| Find folder by name under root | `gws drive files list --params '{"q": "name=\"<name>\" and \"<root_id>\" in parents and mimeType=\"application/vnd.google-apps.folder\" and trashed=false"}'` |
| List images in folder | `gws drive files list --params '{"q": "\"<folder_id>\" in parents and trashed=false", "fields": "files(id,name,mimeType)"}'` |
| Download file | `gws drive files get --fileId <id> --output <local_path>` |
| Upload file | `gws drive +upload <local_path> --parent <folder_id> --name <filename>` |
| Delete file | `gws drive files delete --fileId <id>` |
| Get folder web link | Constructed from folder ID: `https://drive.google.com/drive/folders/<folder_id>` |

Supported image MIME types accepted: `image/jpeg`, `image/png`. Files with other MIME types are silently skipped.

### 5. Video Generator (`tools/video_generator.py`)

**Protocol:**

```python
from typing import Protocol
from pathlib import Path
from dataclasses import dataclass, field

@dataclass
class VideoConfig:
    width: int = 1080
    height: int = 1920
    fps: int = 30
    seconds_per_photo: int = 4
    crossfade_duration: float = 0.5
    bitrate: str = "3M"

class VideoGenerator(Protocol):
    def generate(self, photos: list[Path], config: VideoConfig, output_path: Path) -> Path:
        ...
```

**`FFmpegVideoGenerator`:**

Builds and runs a single FFmpeg command. Filter graph per photo:

```
[{i}:v]scale={W}:{H}:force_original_aspect_ratio=increase,
       crop={W}:{H},setsar=1,fps={fps}[v{i}]
```

Crossfade chain (N photos → N−1 xfade transitions):

```
[v0][v1]xfade=transition=fade:duration={xfade}:offset={offset_0}[x01];
[x01][v2]xfade=transition=fade:duration={xfade}:offset={offset_1}[x12];
...
[x{N-3}{N-2}][v{N-1}]xfade=transition=fade:duration={xfade}:offset={offset_N-2}[xout]
```

Offset formula: `offset[i] = (i + 1) × (seconds_per_photo − crossfade_duration)`

Output flags: `-map [xout] -c:v libx264 -preset medium -b:v {bitrate} -an -r {fps} -pix_fmt yuv420p`

For a single photo (N=1), xfade is skipped; the image is encoded directly with `-t {seconds_per_photo}`.

Raises `VideoGenerationError` (a custom exception) if FFmpeg exits non-zero, including the stderr in the message.

### 6. Telegram API Wrapper (`tools/telegram_api.py`)

Direct HTTP calls to the Telegram Bot API using `requests`. Used for operations OpenClaw's `message send` cannot handle.

| Function | Telegram API Method |
|----------|-------------------|
| `send_message_with_buttons(chat_id, text, buttons)` | `sendMessage` with `reply_markup` (inline keyboard) |
| `answer_callback_query(callback_query_id)` | `answerCallbackQuery` (dismisses spinner on button tap) |
| `get_updates(offset)` | `getUpdates?offset={offset}&timeout=0` |

OpenClaw's `openclaw message send` continues to be used for plain-text messages (errors, status updates) — consistent with Feature 001.

Bot token is read from `TELEGRAM_BOT_TOKEN` in `.env`.

### 7. State Manager (`tools/state.py`)

Reads and writes `data/photo-agent/state.json`. All read-modify-write operations hold an `fcntl.flock` exclusive lock.

**`state.json` schema:**

```json
{
  "telegram_update_offset": 0,
  "pending_approval": null
}
```

When a video is awaiting approval, `pending_approval` holds:

```json
{
  "project_name": "kitchen_remodel",
  "drive_folder_id": "1abc...",
  "drive_video_file_id": "1xyz...",
  "drive_folder_link": "https://drive.google.com/drive/folders/1abc...",
  "video_local_path": "/Users/.../data/photo-agent/tmp/kitchen_remodel/...",
  "telegram_message_id": 42,
  "triggered_at": "2026-05-12T14:32:00Z"
}
```

**Functions:**

- `get_pending_approval() -> dict | None`
- `set_pending_approval(record: dict) -> None`
- `clear_pending_approval() -> None`
- `get_telegram_offset() -> int`
- `set_telegram_offset(offset: int) -> None`

### 8. Local Logger (`tools/logger.py`)

Appends pipe-delimited lines to `logs/photo-agent.log`:

```
2026-05-12 14:32 | COMMAND      | project=kitchen_remodel
2026-05-12 14:32 | DOWNLOADED   | project=kitchen_remodel count=6
2026-05-12 14:33 | GENERATED    | project=kitchen_remodel duration_sec=22 size_bytes=9437184
2026-05-12 14:33 | UPLOADED     | project=kitchen_remodel drive_file_id=1xyz...
2026-05-12 14:33 | APPROVAL_REQ | project=kitchen_remodel message_id=42
2026-05-12 14:35 | APPROVED     | project=kitchen_remodel
2026-05-12 14:35 | REJECTED     | project=kitchen_remodel
2026-05-12 14:32 | ERROR        | project=kitchen_remodel phase=generate detail="ffmpeg exited 1: ..."
```

---

## Data Flow

### A. `process_photos.py`

```
1. _load_env()
2. _acquire_run_lock()          — fcntl.flock on data/photo-agent/run.lock
3. state.get_pending_approval() — if not None: Telegram error + exit
4. drive.find_folder(project_name, root_folder_id)
     → not found: Telegram error + exit
5. drive.list_photos(folder_id)
     → filter to image/* MIME types, sort by name, validate 2–30 count
     → out of range: Telegram error + exit
6. Clear and recreate tmp/<project_name>/
7. For each photo: drive.download(file_id, local_path)
     → download failure: Telegram error + abort + exit
8. photos = scrub(photos)       — no-op this phase
9. config = VideoConfig(seconds_per_photo=SECONDS_PER_PHOTO, ...)
   output_path = tmp/<project_name>/<project_name>_<YYYYMMDD_HHMMSS>.mp4
   FFmpegVideoGenerator().generate(photos, config, output_path)
     → VideoGenerationError: Telegram error + exit
10. drive_video_file_id = drive.upload(output_path, folder_id, filename)
      → upload failure: Telegram error + retain local file + exit
11. folder_link = drive.folder_link(folder_id)
12. duration_sec = len(photos) * secs - (len(photos) - 1) * xfade
    msg_id = telegram_api.send_message_with_buttons(
        chat_id, approval_text(project_name, len(photos), duration_sec, folder_link),
        buttons=[("✅ Approve", "approve"), ("❌ Reject", "reject")]
    )
13. state.set_pending_approval({project_name, folder_id, drive_video_file_id,
                                 folder_link, output_path, msg_id, triggered_at})
14. logger.log_approval_req(project_name, msg_id)
15. Release lock
```

### B. `check_approval.py`

```
1. _load_env()
2. record = state.get_pending_approval()
   → None: exit (nothing pending)
3. offset = state.get_telegram_offset()
4. updates = telegram_api.get_updates(offset)
5. new_offset = max(u["update_id"] for u in updates) + 1 if updates else offset
6. Find callback_query where message.message_id == record["telegram_message_id"]
   → not found: state.set_telegram_offset(new_offset) + exit
7. telegram_api.answer_callback_query(callback_query_id)  — dismiss spinner
8. action = callback_data  ("approve" or "reject")

   If "approve":
     a. gws gmail +send approval email to ADMIN_EMAIL
     b. openclaw message send Telegram confirmation
     c. Delete local video file
     d. logger.log_approved(project_name)

   If "reject":
     a. drive.delete(record["drive_video_file_id"])
     b. Delete local video file
     c. openclaw message send Telegram rejection notice
     d. logger.log_rejected(project_name)

9. state.clear_pending_approval()
10. state.set_telegram_offset(new_offset)
```

---

## File Structure

```
clients/_demo/src/photo-agent/
  SKILL_process_photos.md      # /process_photos skill definition
  SKILL_check_approval.md      # /check_approval skill definition
  scripts/
    __init__.py
    process_photos.py
    check_approval.py
  tools/
    __init__.py
    video_generator.py         # VideoGenerator protocol + FFmpegVideoGenerator
    drive.py                   # gws Drive wrapper
    telegram_api.py            # Direct Telegram Bot API (inline keyboards, getUpdates)
    state.py                   # state.json manager
    logger.py                  # photo-agent.log appender
  tests/
    __init__.py
    test_process_photos.py
    test_check_approval.py
    test_video_generator.py
    test_drive.py
    test_telegram_api.py
    test_state.py
    test_logger.py
  .env.example
  requirements.txt
```

**Runtime data on Mac Mini (not committed):**

```
~/src/fieldkit/
  data/photo-agent/
    state.json                 # pending approval record + Telegram update offset
    run.lock                   # concurrent-run guard
    tmp/
      <project_name>/          # downloaded photos + generated video (transient)
  logs/
    photo-agent.log            # append-only event log
```

---

## Configuration (`.env`)

Extends the existing `.env` in `clients/_demo/src/photo-agent/`. Values shared with Feature 001 (e.g. `ADMIN_TELEGRAM_CHAT_ID`) are duplicated here — each agent owns its own `.env`.

| Variable | Description | Default |
|----------|-------------|---------|
| `AGENT_EMAIL` | Gmail address used to send the approval email | — |
| `ADMIN_EMAIL` | Recipient of the approval email | — |
| `ADMIN_TELEGRAM_CHAT_ID` | Admin's Telegram chat ID | — |
| `TELEGRAM_BOT_TOKEN` | Bot token for direct Bot API calls (required — OpenClaw does not expose this for script use) | — |
| `DRIVE_ROOT_FOLDER_ID` | Google Drive ID of the folder containing project subfolders | — |
| `SECONDS_PER_PHOTO` | Display duration per photo | `4` |
| `VIDEO_TMP_DIR` | Local temp directory for photos and generated video | `data/photo-agent/tmp` |

---

## Setup Checklist (Mac Mini)

- [ ] OpenClaw installed and running
- [ ] OpenClaw Telegram channel configured (existing from Feature 001)
- [ ] `gws` re-authenticated with Drive scope added:
      `gws auth login --account $AGENT_EMAIL --scopes drive`
- [ ] `ffmpeg` installed: `brew install ffmpeg`
- [ ] `pip install requests` (or added to `requirements.txt` and installed)
- [ ] Runtime directories created:
      `mkdir -p ~/src/fieldkit/data/photo-agent/tmp ~/src/fieldkit/logs`
- [ ] `.env` created from `.env.example` and populated (including `TELEGRAM_BOT_TOKEN` and `DRIVE_ROOT_FOLDER_ID`)
- [ ] `/process_photos` skill installed in OpenClaw skill directory
- [ ] `/check_approval` skill installed in OpenClaw skill directory
- [ ] Approval cron job registered (heredoc command in Component 3 above)
- [ ] Admin creates Drive root folder and notes its ID for `.env`

---

## Deferred

**Music:** AAC audio codec is already specified in the FFmpeg output flags (`-an` silences it). To add music in a future phase: replace `-an` with `-i music.aac -shortest -c:a aac` and add the audio file to the skill directory. No other changes required.

**Privacy scrubbing:** The `scrub(photos)` no-op is already in the pipeline between download and generation. Activate by replacing the no-op body with metadata stripping (e.g. `piexif` for JPEG EXIF) and/or face blurring (e.g. OpenCV). No pipeline restructuring required.

**Shotstack / Kling swap:** Replace `FFmpegVideoGenerator` with a new class implementing `VideoGenerator` protocol. `process_photos.py` constructs the generator once and passes it into the pipeline — the swap is one line.

**Rejection reason capture:** In a future phase, replace the ❌ Reject button with a "Reject" flow that asks a follow-up question via Telegram before finalising. `check_approval.py` would enter a secondary wait state for the reason text. State schema already has room for a `rejection_reason` field.
