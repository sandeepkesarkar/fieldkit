# Feature 004 — Data Model

**Feature:** End-to-End Test Rig
**Generated:** 2026-06-20

---

## Entities

### TestRun

A single execution of the e2e test rig, identified by a timestamp-based project name.

| Field | Type | Notes |
|-------|------|-------|
| `project_name` | `str` | Format: `e2e-test-YYYYMMDD-HHMMSS`. Unique per run. Used as Drive folder name, state.json project key, and facebook_state project key. |
| `drive_folder_id` | `str` | Drive folder ID created under `DRIVE_ROOT_FOLDER_ID`. Populated after Stage 1. |
| `fb_post_id` | `str \| None` | Facebook post ID. Populated after Stage 5. Used for cleanup. |
| `started_at` | `datetime` | UTC timestamp when the test rig started. |

**Validation:** `project_name` must match `^e2e-test-\d{8}-\d{6}$`.

---

### PipelineStage

One of five checkpoints the test rig tracks. Not persisted to disk — held in memory during a single test run.

| Field | Type | Notes |
|-------|------|-------|
| `number` | `int` | 1–5 |
| `name` | `str` | e.g., "Drive upload", "Video generation", "Telegram approval sent", "Approval received", "Facebook post live" |
| `status` | `Literal["pending", "passed", "failed", "timed_out"]` | |
| `started_at` | `datetime \| None` | When the stage started polling |
| `completed_at` | `datetime \| None` | When the stage passed or failed |
| `error` | `str \| None` | Error message if status is failed or timed_out |

---

### SyntheticContent

A set of JPEG clock frames generated locally for the test run, then uploaded to Drive for `process_photos.py` to assemble.

| Field | Type | Notes |
|-------|------|-------|
| `frames_dir` | `Path` | Local temp directory holding `frame_001.jpg` … `frame_NNN.jpg` |
| `n_frames` | `int` | Number of frames generated; derived from `--duration` and `SECONDS_PER_PHOTO` |
| `spp_effective` | `int` | `SECONDS_PER_PHOTO` value passed to `process_photos.py` subprocess env |
| `drive_folder_id` | `str` | Drive folder ID created under `DRIVE_ROOT_FOLDER_ID` (used for cleanup) |
| `drive_file_ids` | `list[str]` | Drive file ID for each uploaded JPEG (retained for audit; folder delete covers cleanup) |
| `start_unix` | `int` | `int(time.time())` captured before FFmpeg call; the `localtime` PTS base so frame 0 shows the real date/time when the test started |

---

## State Files (existing — read by test rig)

The test rig reads (never writes) these files:

### state.json — `data/photo-agent/state.json`

Read via `tools/state.get_pending_approval()`. Test rig checks:
- Stage 2+3: `record["project_name"] == test_name` (pending_approval appeared)
- Stage 4: `record is None` (pending_approval cleared after approval)

### facebook_state.json — `data/photo-agent/facebook_state.json`

Read via `tools/facebook_state.get_pending_upload()`. Test rig checks:
- Stage 4: `record["project_name"] == test_name` (facebook upload enqueued)
- Stage 5: `record["status"] == "published"` (facebook upload complete)

---

## State Transitions

```
TestRun lifecycle:
  CREATED → [Stage 1] DRIVE_SEEDED → [Stage 2+3] TELEGRAM_SENT
  → [Stage 4, manual] APPROVED → [Stage 5] PUBLISHED
  → (optional) CLEANED_UP

PipelineStage lifecycle:
  pending → passed
          → failed (error encountered)
          → timed_out (timeout elapsed)
```
