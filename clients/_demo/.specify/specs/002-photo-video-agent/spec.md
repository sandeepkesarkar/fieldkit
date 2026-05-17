# 002 — Photo Video Agent

**Status:** Spec
**Type:** Client feature (N≥2 rule — not yet extracted to platform)
**Last Updated:** 2026-05-12

---

## Purpose

The Photo Video Agent turns a batch of project photos, uploaded by the admin to a dedicated Google Drive folder, into a short video suitable for posting on Facebook, Instagram, and WhatsApp. The admin triggers processing via Telegram, reviews and approves the generated video, and receives the final video link by email.

This is the second step in the social media automation pipeline:

```
002 — Photo intake + video generation  ← this feature
003 — Caption generation
004 — Approval workflow (post-caption)
005 — Social media posting
```

---

## Scope

**In scope:**
- Accepting an admin Telegram command naming the Drive project folder to process
- Listing and downloading photos from that Drive folder to the Mac Mini
- Generating a short video from the photos using FFmpeg
- Uploading the generated video back to the same Drive project folder
- Sending the admin a Telegram approval request with inline Approve / Reject buttons
- On approval: emailing the admin a Drive folder link and sending a Telegram confirmation
- On rejection: removing the video from Drive and local storage, notifying admin via Telegram

**Out of scope:**
- Privacy or metadata scrubbing of photos — pipeline is designed to accommodate this in a future phase; the scrub step is a no-op placeholder in this phase
- Background music — AAC audio codec is reserved; video is silent in this phase
- Text overlays or branding on the video
- Before/after video formats
- Posting the video to any social media platform (Feature 005)
- Caption generation (Feature 003)
- Capturing or acting on rejection reasons — admin curates the Drive folder and re-triggers
- Multi-project processing in a single command

---

## Actors

- **Admin** — the business owner or designated operator; uploads photos to Drive, sends the Telegram command, approves or rejects the video
- **Agent** — the FieldKit automation system running on the Mac Mini via OpenClaw; downloads photos, generates the video, manages the approval dialog, delivers on approval

---

## Configuration Interface

All parameters are stored in `.env` on the Mac Mini. None are committed to the repo.

| Parameter | Description | Default |
|-----------|-------------|---------|
| `drive_root_folder_id` | Google Drive ID of the root folder that contains project subfolders | — |
| `admin_email` | Address to receive the approved video link | — |
| `admin_telegram_chat_id` | Admin's Telegram chat ID | — |
| `seconds_per_photo` | How long each photo is displayed in the video | `4` |
| `video_tmp_dir` | Local directory for temporary video files during processing | `data/photo-agent/tmp/` |

---

## Drive Folder Convention

The admin creates one subfolder per project under the root folder. The subfolder name is the project name — exactly as passed to the Telegram command.

```
<drive_root_folder>/
├── kitchen_remodel/          ← project folder
│   ├── 01_demo_before.jpg
│   ├── 02_demo_progress.jpg
│   └── 03_demo_finished.jpg
└── bathroom_renovation/      ← another project folder
    └── ...
```

The agent does not create or rename project folders. Folder creation is the admin's responsibility.

---

## Core Behavior

### 1. Command Trigger

Admin sends `/process_photos <project_name>` via Telegram (e.g. `/process_photos kitchen_remodel`).

- The agent extracts the project name from the command
- If no project name is provided, the agent replies:
  ```
  Please provide a project name — e.g. /process_photos kitchen_remodel
  ```
- The agent looks for a Drive subfolder whose name exactly matches the project name under `drive_root_folder_id`
- If no matching folder is found, the agent replies:
  ```
  ✗ No Drive folder found for project 'kitchen_remodel'
  Create a folder with that exact name and add photos, then try again.
  ```
- The command is available only to the admin (enforced by `admin_telegram_chat_id`)

### 2. Photo Discovery

The agent lists all files in the project Drive folder and filters to supported image types: `.jpg`, `.jpeg`, `.png`.

- Files are sorted **alphabetically by filename** — the sort order determines the sequence in the video
- If fewer than 2 photos are found, the agent replies:
  ```
  ✗ At least 2 photos are required to generate a video.
  'kitchen_remodel' has <N> image(s). Add photos and try again.
  ```
- Subfolders within the project folder are ignored

### 3. Photo Download

All filtered photos are downloaded to `video_tmp_dir/<project_name>/` on the Mac Mini.

- The download directory is created fresh each run (existing files from prior runs are deleted first)
- If any individual photo fails to download, the agent aborts and replies:
  ```
  ✗ Failed to download photo '<filename>' — processing aborted.
  ```

### 4. Privacy Scrubbing (no-op placeholder)

A dedicated scrub step exists in the pipeline between download and video generation. In this phase it does nothing. Its signature is:

```python
def scrub(photos: list[Path]) -> list[Path]:
    return photos  # no-op: privacy scrubbing not yet implemented
```

The step is retained so future phases can activate metadata stripping, face blurring, or other scrubbing without restructuring the pipeline.

### 5. Video Generation

The agent generates the video using FFmpeg via a `VideoGenerator` protocol. The protocol is:

```python
class VideoGenerator(Protocol):
    def generate(self, photos: list[Path], config: VideoConfig) -> Path:
        ...
```

The `FFmpegVideoGenerator` is the implementation used in this phase.

**Video specification:**

| Property | Value |
|----------|-------|
| Container | MP4 |
| Video codec | H.264 (libx264) |
| Resolution | 1080 × 1920 (portrait, 9:16) |
| Frame rate | 30 fps |
| Audio codec | AAC (silent — no audio track in this phase) |
| Photo duration | `seconds_per_photo` (default 4 sec) |
| Transition | Crossfade between each photo (FFmpeg `xfade` filter) |
| Scaling | Each photo is scaled and center-cropped to fill 1080 × 1920 without letterboxing |
| Bitrate | ~3 Mbps target — keeps a 30-second video under 16 MB (WhatsApp standard limit) |

Output filename: `<project_name>_<YYYYMMDD_HHMMSS>.mp4` (timestamp = generation time in UTC)

If video generation fails, the agent replies:
```
✗ Video generation failed for 'kitchen_remodel' — <reason>
```

### 6. Video Upload

The generated video is uploaded to the same Drive project folder it was sourced from.

- On upload success the agent retains the Drive file ID and the folder's shareable link
- If upload fails, the agent replies:
  ```
  ✗ Failed to upload video to Drive — local file retained at <path>
  ```
  and does not proceed to the approval step

### 7. Approval Request

The agent sends a Telegram message to the admin:

```
📹 Video ready for review — kitchen_remodel
📁 View in Drive: <drive_folder_link>

Photos: 6
Duration: 24 sec

Tap to respond:
```

The message includes two inline keyboard buttons:

| Button | Label |
|--------|-------|
| Approve | ✅ Approve |
| Reject | ❌ Reject |

The agent waits indefinitely for a response. There is no timeout and no auto-approval.

### 8. Approval Flow

**On ✅ Approve:**

1. Agent sends an email to `admin_email`:

   ```
   Subject: ✅ Approved — kitchen_remodel

   Your video for kitchen_remodel has been approved.

   View and download: <drive_folder_link>

   — FieldKit
   ```

2. Agent sends a Telegram confirmation:

   ```
   ✅ Approved — kitchen_remodel
   Video link sent to admin@example.com
   ```

3. Agent deletes the local temporary video file
4. Agent records the approval in the local log

**On ❌ Reject:**

1. Agent deletes the generated video from Drive
2. Agent deletes the local temporary video file
3. Agent sends a Telegram notification:

   ```
   ❌ Rejected — kitchen_remodel
   Video removed. Update the photos in Drive and run /process_photos kitchen_remodel again when ready.
   ```

4. Agent records the rejection in the local log
5. Agent returns to idle — no further action until the admin re-triggers

### 9. Local Logging

Every event is appended to a local log on the Mac Mini.

| Event | Fields logged |
|-------|--------------|
| Command received | Timestamp, project name |
| Photos discovered | Timestamp, project name, count, filenames |
| Photos downloaded | Timestamp, project name, count |
| Video generated | Timestamp, project name, duration (sec), file size (bytes), output path |
| Video uploaded to Drive | Timestamp, project name, Drive file ID |
| Approval requested | Timestamp, project name |
| Video approved | Timestamp, project name |
| Video rejected | Timestamp, project name |
| Error (any phase) | Timestamp, project name, phase name, error detail |

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| No project name in command | Telegram reply with usage example |
| Drive folder not found | Telegram error; idle |
| Fewer than 2 photos in folder | Telegram error; idle |
| Photo download fails | Telegram error; abort; idle |
| FFmpeg not installed | Telegram error: "FFmpeg not found — check Mac Mini setup"; idle |
| Video generation fails | Telegram error with reason; local log; idle |
| Drive upload fails | Telegram error; retain local file; idle |
| Admin never responds to approval | Idle indefinitely — no timeout, no auto-approve |
| Email send fails on approval | Telegram error: "Video approved but email delivery failed — Drive link: <link>"; log |

---

## Testing Requirements

Implementation follows test-driven development. Tests are written before or alongside code, not after.

### Unit Tests (external dependencies mocked)

- Photos are sorted alphabetically by filename before video generation
- Files with unsupported extensions are excluded from photo list
- FFmpeg command is constructed correctly for a given photo list and `VideoConfig`
- Output filename uses correct project name and UTC timestamp format
- Drive folder lookup returns the correct folder by name
- Drive folder lookup raises a clear error when no matching folder exists
- Approval Telegram message is formatted correctly (text + button labels)
- Approval triggers email send and Telegram confirmation
- Rejection deletes Drive file and sends correct Telegram notification
- Local temp directory is cleared before each new run

### Integration Tests (real or realistic test doubles)

- Full happy path: `/process_photos <name>` → photos downloaded → video generated → video in Drive → admin approves → email delivered + Telegram confirmation sent
- Rejection path: admin rejects → video removed from Drive + local → Telegram rejection notification sent
- No Drive folder: command → Telegram error; no further action
- Empty / single-photo Drive folder: command → Telegram error; no further action
- Photo download failure: partial download → Telegram error; abort

---

## Success Criteria

- [ ] Admin sends `/process_photos <name>` → video appears in Drive folder within 2 minutes → Telegram approval request received with inline Approve / Reject buttons
- [ ] Admin taps ✅ Approve → email with Drive folder link received at `admin_email` + Telegram confirmation sent
- [ ] Admin taps ❌ Reject → video removed from Drive + local storage → Telegram rejection notification received
- [ ] Photos appear in the video in alphabetical filename order
- [ ] Video is MP4, 1080 × 1920, 9:16, 30 fps, H.264, silent AAC, with crossfade transitions
- [ ] A 5-photo video at 4 sec/photo is under 16 MB (WhatsApp compatibility)
- [ ] `VideoGenerator` is implemented as a protocol — the FFmpeg implementation can be swapped without touching pipeline code
- [ ] Privacy scrub step exists as a no-op in the pipeline — activatable without restructuring
- [ ] No Drive credentials, tokens, or folder IDs are hardcoded — all come from `.env`
- [ ] All events (command received, photos downloaded, video generated, approval, rejection, errors) are logged locally with timestamps
