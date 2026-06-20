# Feature 004 — Research Findings

**Status:** Complete — no open NEEDS CLARIFICATION items
**Generated:** 2026-06-20

---

## 1. Drive Folder Creation

**Decision:** Add `drive.create_folder(name, parent_id)` to `drive.py`.

Drive v3 REST API creates a folder with a single POST:

```
POST https://www.googleapis.com/drive/v3/files
Authorization: Bearer {access_token}
Content-Type: application/json

{
  "name": "{name}",
  "mimeType": "application/vnd.google-apps.folder",
  "parents": ["{parent_id}"]
}
```

Returns `{"id": "{folder_id}", ...}`. The existing `_drive_get` and `upload` helpers handle auth; the new function needs a `_drive_post` call (same pattern). The response is checked for HTTP 200 and the `id` key is extracted.

**Alternatives considered:** Reuse `upload()` — not applicable (upload is for file content). Direct call in the test rig script — violates DRY; drive.py is the single integration point.

---

## 2. Synthetic Clock Frame Generation

**Decision:** Generate all JPEG frames in a single FFmpeg command at `rate=1`, using the `localtime` PTS expansion so each frame automatically shows `start_unix + i` seconds — no Python loops, no per-frame subprocesses.

```bash
ffmpeg \
  -f lavfi -i "color=c=#1D4ED8:size=1080x1920:rate=1" \
  -vf "drawtext=fontfile=/System/Library/Fonts/Helvetica.ttc:\
fontsize=80:fontcolor=white:x=(w-text_w)/2:y=h*0.35:\
text='FieldKit E2E Test',\
drawtext=fontfile=/System/Library/Fonts/Helvetica.ttc:\
fontsize=120:fontcolor=white:x=(w-text_w)/2:y=h*0.50:\
text='%{pts\\:localtime\\:START_UNIX\\:%m/%d/%Y}',\
drawtext=fontfile=/System/Library/Fonts/Helvetica.ttc:\
fontsize=140:fontcolor=white:x=(w-text_w)/2:y=h*0.62:\
text='%{pts\\:localtime\\:START_UNIX\\:%H\\:%M\\:%S}'" \
  -frames:v N_FRAMES \
  -y /tmp/e2e-test-YYYYMMDD-HHMMSS/frame_%03d.jpg
```

At `rate=1`, PTS advances by 1 second per frame. Frame 0 shows `start_unix + 0s`, frame 1 shows `start_unix + 1s`, etc. Because `START_UNIX` is Python's `int(time.time())` captured before the subprocess call, each frame shows the real wall-clock date/time in `MM/DD/YYYY HH:MM:SS` format, advancing by one second per frame.

**Output:** `frame_001.jpg` through `frame_NNN.jpg` — named so Drive's alphabetical sort preserves the clock order.

**Frame count and `spp` calculation:**

`process_photos.py` assembles frames into a slideshow with `SECONDS_PER_PHOTO` (env, default 4) and `crossfade_duration=0.5` (VideoConfig constant). The duration formula is:

```
actual_duration = n * spp - (n - 1) * xfade
```

The test rig derives `n_frames` and `spp_effective` from `--duration`:

```python
xfade = 0.5  # VideoConfig constant
spp_base = int(os.environ.get("SECONDS_PER_PHOTO", "4"))

n_frames = math.ceil((duration - xfade) / (spp_base - xfade))

if n_frames > 30:          # process_photos.py _MAX_PHOTOS = 30
    n_frames = 30
    spp_effective = math.ceil((duration + (n_frames - 1) * xfade) / n_frames)
else:
    spp_effective = spp_base
```

`spp_effective` is passed as `SECONDS_PER_PHOTO={spp_effective}` in the subprocess env when calling `process_photos.py`. The actual video will be approximately `--duration` seconds; small rounding differences are acceptable.

**Examples (default spp=4):**

| `--duration` | n_frames | spp_effective | Actual video |
|-------------|----------|---------------|--------------|
| 30s | 9 | 4 | ~32s |
| 60s | 17 | 4 | ~60s |
| 120s | 30 | 5 | ~135s |
| 300s | 30 | 11 | ~315s |

**Font note:** `/System/Library/Fonts/Helvetica.ttc` is available on macOS. If absent, fall back to FFmpeg's built-in default font (omit the `fontfile=` argument).

**Alternatives considered:** One FFmpeg subprocess per frame — slow for 30 frames, no benefit over batch. Python Pillow — new pip dependency. Pre-canned static images — no clock progression. Direct MP4 generation (bypassing process_photos.py) — forks the production workflow and doesn't test the image-to-video pipeline.

---

## 3. Facebook Post Deletion

**Decision:** Add `facebook_api.delete_post(page_access_token, post_id)` to `facebook_api.py`.

Facebook Graph API v25.0 deletes a post with:

```
DELETE https://graph.facebook.com/v25.0/{post_id}?access_token={page_token}
```

Returns `{"success": true}` on success. Returns error code 100 with message "Object with ID... does not exist" if already deleted — this is the "already deleted" case that the spec requires to be handled as a warning (SC-002 in spec: cleanup continues without error).

**Alternatives considered:** Direct requests call in the test rig — violates DRY.

---

## 4. Bug in check_approval.py — Local File Deleted Before FB Upload

**Finding:** `check_approval.py` line 389 calls `_delete_local_file(video_local_path, ...)` *before* line 395 calls `_enqueue_facebook_upload(project_name, video_local_path, ...)`. However, `upload_facebook.py` lines 91–94 check `Path(video_path).exists()` and mark the job as `failed` if the file is missing.

**Impact:** The full pipeline has never successfully completed without manual seeding of `facebook_state.json`. Smoke testing (Feature 003) bypassed `check_approval.py` entirely by writing the state file directly.

**Fix:** Move `_delete_local_file()` out of `check_approval.py`'s approve path. Let `upload_facebook.py` delete the local file after a successful upload. This aligns with the principle that each script is responsible only for its own stage — `check_approval.py` does not own the video file lifetime after enqueuing the upload.

**Rationale for fixing in Feature 004:** The e2e test rig exercises the full pipeline and will expose this failure on Stage 5. Fixing it is in-scope because it unblocks the feature objective (SC-003: no false positives).

---

## 5. State File Polling Strategy

**Decision:** Simple blocking poll — `time.sleep(10)` in a while loop, check condition, repeat until success or timeout.

Each stage has its own condition:

| Stage | Condition to check | File |
|-------|-------------------|------|
| 1 | Drive upload returns folder_id | (synchronous — no poll) |
| 2+3 | `state.get_pending_approval().project_name == test_name` | `state.json` |
| 4 | `state.get_pending_approval() is None` AND `facebook_state.get_pending_upload().project_name == test_name` | both state files |
| 5 | `facebook_state.get_pending_upload().status == "published"` | `facebook_state.json` |

Poll interval: 10 seconds. Default timeout per stage: 3 minutes (FR-005). Stage 4 default: 10 minutes (allows 2 cron cycles + human reaction time; overridable via `--approval-timeout`).

**Alternatives considered:** inotify / fsevents file watching — overkill; adds OS-specific code. Async/threading — unnecessary complexity for a CLI tool.

---

## 6. process_photos.py Invocation

**Decision:** Call `process_photos.py` as a direct subprocess from the repo root. This is how OpenClaw triggers it; the test rig bypasses the chat layer but runs the identical code path.

```python
env = os.environ.copy()
env["SECONDS_PER_PHOTO"] = str(spp_effective)   # from §2 calculation
subprocess.run(
    [sys.executable,
     "clients/_demo/src/photo-agent/scripts/process_photos.py",
     "--project", test_name],
    env=env,
    check=True,
    timeout=300,
)
```

`SECONDS_PER_PHOTO` is overridden per-call (not written to `.env`) so real projects are unaffected.

**Pre-flight check:** Verify `state.get_pending_approval() is None` before generating frames — if another project is awaiting approval, `process_photos.py` will abort with "already awaiting approval."

**After the subprocess returns:** `state.json` will have `pending_approval.project_name == test_name` and the Telegram approval message will already have been sent. The test rig polls to confirm this before printing Stage 2+3 status.

---

## 7. Guard Against Concurrent Real Workflows

**Decision:** Use a namespaced project name `e2e-test-YYYYMMDD-HHMMSS` (regex: `^e2e-test-\d{8}-\d{6}$`) to isolate test runs. Pre-flight check verifies no other approval is pending before starting.

The `pending_approval` structure in `state.json` tracks only ONE pending approval. If a real project is awaiting approval when the test runs, process_photos.py will abort early. The test rig detects this on the pre-flight check and exits with a clear message rather than silently failing later.
