# 004 — End-to-End Test Rig: Technical Plan

**Status:** Technical Planning Complete
**Spec:** [`spec.md`](spec.md)
**Research:** [`research.md`](research.md)
**Data Model:** [`data-model.md`](data-model.md)
**Sequence Diagram:** [`sequence-diagram.md`](sequence-diagram.md)
**Last Updated:** 2026-06-20

---

## Stack

| Concern | Solution | Rationale |
|---------|----------|-----------|
| CLI script | Python 3.11, stdlib only | No new dependencies; consistent with existing scripts |
| Clock frame generation | FFmpeg lavfi `rate=1` + drawtext `%{pts\:localtime\:...}` | Single command, all frames at once; real-time MM/DD/YYYY HH:MM:SS per frame |
| Drive folder creation | New `drive.create_folder()` in drive.py | Single Drive integration point pattern |
| Drive image upload | Existing `drive.upload()` + new `content_type` param | `upload()` currently hardcodes `video/mp4`; add optional param for JPEG |
| process_photos.py | Direct subprocess with `SECONDS_PER_PHOTO` override | Tests the real pipeline; no workflow fork |
| Facebook post deletion | New `facebook_api.delete_post()` in facebook_api.py | Single FB integration point pattern |
| State polling | `time.sleep(10)` loop, configurable timeout | Simplest correct approach |

---

## Architecture

`scripts/run_e2e_test.py` is a single, sequentially-structured CLI script. It owns a test run from frame generation through Facebook post verification. Five stage functions execute in order; each prints a timestamped pass/fail line and exits non-zero on failure or timeout. The script is entirely self-contained — it reads from `.env`, calls existing tools (`drive.py`, `state.py`, `facebook_state.py`, `facebook_api.py`), invokes FFmpeg via subprocess to generate JPEG clock frames, then calls `process_photos.py` via subprocess to assemble the video and send the Telegram approval. From that point the production cron jobs (`check_approval.py`, `upload_facebook.py`) run unmodified. No cron entry is needed for the test rig itself.

A targeted bug fix in `check_approval.py` and `upload_facebook.py` (moving local file deletion to the upload success path) is required before Stage 5 can succeed — this is treated as in-scope for this feature.

---

## Sequence Diagram

See [`sequence-diagram.md`](sequence-diagram.md) for the full Mermaid diagram.

---

## Constitution Check

- [x] **Privacy**: Synthetic test content (solid-colour PNG frames) contains no PII. Drive folder and Facebook post are namespaced to `e2e-test-YYYYMMDD-HHMMSS`. Cleanup flag removes them after the run.
- [x] **HITL**: Human approval gate is preserved. The test rig pauses at Stage 4 and waits for the admin to tap Approve in Telegram. It does not auto-approve.
- [x] **Budget**: No AI API calls. No unbounded loops — each stage has a hard timeout (default 3 min, Stage 4 default 10 min).
- [x] **Ownership**: Reads existing `.env` and credentials. No new external services. Test artifacts stored only in the client's Drive and on the Mac Mini.
- [x] **Test fidelity gate**: The rig only reports Stage 5 success after `facebook_state.get_pending_upload().status == "published"` — never on assumption. (SC-003)

---

## Technical Context

**Language/Version:** Python 3.11
**Primary dependencies:** `requests`, `python-dotenv`, `subprocess` (stdlib) — all already present
**New functions:** `drive.create_folder()`, `facebook_api.delete_post()`
**Bug fix:** Move `_delete_local_file()` from `check_approval.py` approve path to `upload_facebook.py` success path
**Storage:** Reads `data/photo-agent/state.json` and `data/photo-agent/facebook_state.json` (existing files, read-only for the test rig)
**Testing:** pytest + pytest-mock (Constitution Gate 5)
**Target platform:** macOS (Mac Mini M-series)
**Project type:** On-demand CLI script (no cron entry)

---

## Implementation Phases

### Phase 0: Research

**Complete.** See [`research.md`](research.md). Key findings:

- Drive folder creation: `POST /drive/v3/files` with `mimeType=application/vnd.google-apps.folder`
- FFmpeg lavfi + drawtext generates timestamp-embedded PNGs without new deps
- Facebook `DELETE /{post_id}` handles cleanup; "not found" is a warning, not an error
- **Bug found:** `check_approval.py` deletes the local video file before `upload_facebook.py` can read it — fix required in Phase 1

**Output:** `research.md` ✅

### Phase 1: Core Implementation

Four distinct implementation tasks, each with tests before code (TDD):

#### 1a. Add `drive.create_folder(name, parent_id)` + `content_type` param to `drive.upload()` in `drive.py`

```python
def create_folder(name: str, parent_id: str) -> str:
    """Create a Drive folder under parent_id. Returns the new folder ID."""
```

- `create_folder(name, parent_id) -> str` — validates name against `_SAFE_FOLDER_NAME_RE`, POSTs with `mimeType=application/vnd.google-apps.folder`, returns `id`. Raises `RuntimeError` on HTTP error.
- `upload(local_path, parent_id, name, content_type="video/mp4") -> str` — adds optional `content_type` parameter (default unchanged for backward compatibility). Test rig passes `content_type="image/jpeg"` when uploading clock frames.

Tests: `tests/test_drive.py` — add tests for `create_folder()` (happy path, HTTP error, name validation) and `upload()` with `content_type="image/jpeg"`.

#### 1b. Add `facebook_api.delete_post(page_access_token, post_id)` to `facebook_api.py`

```python
def delete_post(page_access_token: str, post_id: str) -> None:
    """Delete a Facebook post by ID. Raises FacebookUploadError on failure.
    
    A 'does not exist' error (code 100) is re-raised — callers handle it as a warning.
    """
```

Tests: `tests/test_facebook_api.py` — mock HTTP, success path, 404-equivalent (code 100).

#### 1c. Fix `check_approval.py` / `upload_facebook.py` local file lifecycle

**check_approval.py**: Remove the `_delete_local_file(video_local_path, project_name)` call from the approve path. The video file must remain on disk until `upload_facebook.py` has successfully uploaded it.

**upload_facebook.py**: After `facebook_state.mark_published(...)`, add:

```python
_delete_local_file(video_path, project_name)
```

Where `_delete_local_file` is extracted to a shared location (or duplicated with the same safety logic from check_approval.py). The delete is best-effort: log on failure, do not raise.

Tests: Update `tests/test_check_approval.py` to verify `_delete_local_file` is NOT called on approval. Update (or add) `tests/test_upload_facebook.py` to verify local file is deleted after successful upload.

#### 1d. Write `scripts/run_e2e_test.py`

```
Usage:
  python3 scripts/run_e2e_test.py
  python3 scripts/run_e2e_test.py --duration 60
  python3 scripts/run_e2e_test.py --cleanup
  python3 scripts/run_e2e_test.py --timeout 180 --approval-timeout 600
  python3 scripts/run_e2e_test.py --dry-run
```

**`--duration`**: desired video length in seconds (default: 30, min: 2, max: 300). Rejected with a clear error outside this range.

**Frame count and `spp` calculation** (see research.md §2 for derivation):
```python
xfade = 0.5
spp_base = int(os.environ.get("SECONDS_PER_PHOTO", "4"))
n_frames = math.ceil((duration - xfade) / (spp_base - xfade))
if n_frames > 30:   # _MAX_PHOTOS in process_photos.py
    n_frames = 30
    spp_effective = math.ceil((duration + (n_frames - 1) * xfade) / n_frames)
else:
    spp_effective = spp_base
```

**FFmpeg command** (generates all `n_frames` JPEG clock frames in one call):
```python
start_unix = int(time.time())
cmd = [
    "ffmpeg", "-f", "lavfi",
    "-i", "color=c=#1D4ED8:size=1080x1920:rate=1",
    "-vf", (
        f"drawtext=fontfile=/System/Library/Fonts/Helvetica.ttc:"
        f"fontsize=80:fontcolor=white:x=(w-text_w)/2:y=h*0.35:"
        f"text='FieldKit E2E Test',"
        f"drawtext=fontfile=/System/Library/Fonts/Helvetica.ttc:"
        f"fontsize=120:fontcolor=white:x=(w-text_w)/2:y=h*0.50:"
        f"text='%{{pts\\:localtime\\:{start_unix}\\:%m/%d/%Y}}',"
        f"drawtext=fontfile=/System/Library/Fonts/Helvetica.ttc:"
        f"fontsize=140:fontcolor=white:x=(w-text_w)/2:y=h*0.62:"
        f"text='%{{pts\\:localtime\\:{start_unix}\\:%H\\:%M\\:%S}}'"
    ),
    "-frames:v", str(n_frames),
    "-y", str(frames_dir / "frame_%03d.jpg"),
]
```

**Stage structure** (each returns `StageResult(passed, elapsed, error)`):

| Stage | Action | Success Condition |
|-------|--------|------------------|
| 1 | FFmpeg generates `n_frames` JPEG clock frames into a local temp dir | No exception; `n_frames` files exist, all non-zero |
| 2 | `drive.create_folder(test_name)`; `drive.upload(frame, content_type="image/jpeg")` × n_frames | No exception; `folder_id` + all `drive_file_ids` returned |
| 3 | `subprocess.run(process_photos.py --project test_name, env={SECONDS_PER_PHOTO: spp_effective})`; poll `state.get_pending_approval()` | `pending_approval.project_name == test_name` (process_photos.py sent Telegram and wrote state) |
| 4 | Print "Tap Approve in Telegram"; poll `state.get_pending_approval() is None` AND `facebook_state.get_pending_upload().project_name == test_name` | Both conditions true (check_approval.py processed the tap) |
| 5 | Poll `facebook_state.get_pending_upload().status == "published"` | Status is `published` (upload_facebook.py completed) |

**Pre-flight checks** (before Stage 1):
- `state.get_pending_approval() is None` — fail fast if another project is awaiting approval
- Required env vars: `FB_PAGE_ACCESS_TOKEN`, `FB_PAGE_ID`, `DRIVE_ROOT_FOLDER_ID`, `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_CHAT_ID`
- FFmpeg is on PATH (`shutil.which("ffmpeg")`)
- `--duration` is in [2, 300]

**Output format** (stdout, one line per event):
```
[14:05:00] Pre-flight checks ✅
[14:05:01] Stage 1/5: Clock frames generated (9 frames, ~32s video) ✅ done (2s)
[14:05:03] Stage 2/5: Drive upload (9 files) ✅ done (14s)
[14:05:17] Stage 3/5: process_photos.py + Telegram sent ✅ done (42s)
[14:05:59] Tap Approve in Telegram to continue (timeout: 10m)
[14:06:55] Stage 4/5: Approval received ✅ (56s)
[14:07:50] Stage 5/5: Facebook post live ✅ (55s)

✅ All stages passed. Total: 2m 50s
Post: https://www.facebook.com/{post_id}
```

**Cleanup** (`--cleanup` flag):
1. `drive.delete(folder_id)` — deletes the entire test Drive folder (Drive deletes contents automatically)
2. `facebook_api.delete_post(page_token, fb_post_id)` — deletes the Facebook test post; "not found" → warning, not error

Tests: `tests/test_run_e2e_test.py` — mock FFmpeg subprocess, `drive.*`, `process_photos.py` subprocess, and `facebook_state.*` reads; test happy path, pre-flight failures, frame count calculation, stage timeout, cleanup success and "already deleted" warning.

**Output:** `scripts/run_e2e_test.py` + `tests/test_run_e2e_test.py`

### Phase 2: Integration

- No cron entry (script is on-demand)
- No OpenClaw config (direct CLI)
- Verify cron jobs for `check_approval.py` and `upload_facebook.py` are still in the crontab (no changes needed, just confirm)
- Run the full test end-to-end against the live environment after all unit tests pass

**Output:** Verified live run + updated adversarial review notes

---

## Project Structure Changes

```
clients/_demo/src/photo-agent/
├── scripts/
│   ├── run_e2e_test.py          ← NEW: test rig script
│   ├── check_approval.py        ← MODIFIED: remove _delete_local_file from approve path
│   └── upload_facebook.py       ← MODIFIED: add _delete_local_file after successful upload
├── tools/
│   ├── drive.py                 ← MODIFIED: add create_folder()
│   └── facebook_api.py          ← MODIFIED: add delete_post()
└── tests/
    ├── test_drive.py            ← MODIFIED: add create_folder() tests
    ├── test_facebook_api.py     ← MODIFIED: add delete_post() tests
    ├── test_check_approval.py   ← MODIFIED: verify _delete_local_file not called on approve
    ├── test_upload_facebook.py  ← MODIFIED: add delete_local_file-after-upload tests
    └── test_run_e2e_test.py     ← NEW: e2e test rig unit tests (mocks FFmpeg + all APIs)
```

---

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Synthetic content type | JPEG frames (not MP4) | Goes through the full Drive → process_photos.py pipeline; no workflow fork |
| Clock frame generation | Single FFmpeg command at rate=1, `%{pts\:localtime\:...}` | All frames in one call; each frame shows correct MM/DD/YYYY HH:MM:SS for its second |
| Video duration | `--duration` flag (default: 30s, min: 2s, max: 300s) | n_frames + spp_effective derived from duration and SECONDS_PER_PHOTO; approximately correct |
| process_photos.py invocation | Direct subprocess with SECONDS_PER_PHOTO override | Tests real pipeline; spp override is per-call, doesn't affect .env |
| Approval step | Manual Telegram tap | Keeps HITL gate representative (Constitution Gate 2) |
| File deletion bug fix | Move deletion to upload_facebook.py success path | Correct lifecycle: each script owns only its own stage |
| Drive upload content_type | Optional param added to drive.upload() | Backward-compatible; test rig passes image/jpeg for frames |
| Drive folder creation | New `drive.create_folder()` in drive.py | Single Drive integration point |
| Stage 4 timeout | 10 minutes default (configurable) | Allows 2 cron cycles + human reaction time |
| Test isolation | Namespaced project name `e2e-test-YYYYMMDD-HHMMSS` | Prevents collision with real pending approvals |
| Cleanup | Optional `--cleanup` flag | Passes without cleanup (SC-004); cleanup is convenience |

---

## Open Questions

None — all NEEDS CLARIFICATION items resolved in research.md and spec clarifications.
