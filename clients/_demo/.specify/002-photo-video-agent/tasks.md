# 002 — Photo Video Agent: Task Breakdown

**Status:** Task Breakdown
**Techplan:** [`techplan.md`](techplan.md)
**Last Updated:** 2026-05-12

Tasks are ordered by dependency. T01–T06 are independent and can be done in any order. T07–T08 depend on T01–T05. T09 depends on T07–T08. T10–T13 are sequential from there.

---

## Definition of Done

A task is complete **only** when its tests pass. No exceptions.

| Task type | Test requirement |
|-----------|-----------------|
| Code (T01–T08) | `pytest` run clean — zero failures, zero errors |
| Config (T06) | Manually verified: all variables present, file loads without error |
| Skills (T09) | `openclaw skills list` shows both `process_photos` and `check_approval` with no load errors |
| Setup (T10) | Every checklist item checked off; gws Drive round-trip verified |
| Smoke tests (T11–T13) | Every numbered step verified and checked off |

**Both the implementer and the assisting AI must run the tests before marking a task done.** If a test fails after implementation, the task is not done — fix and re-run before moving to the next task.

**Gate rules:**
- Do not start T07 or T08 until `pytest tests/` is clean across T01–T05
- Do not start T09 until `pytest tests/` is clean across T07–T08
- Do not start T10 until T09 is verified in OpenClaw
- Do not start T11 until T10 checklist is fully checked off
- T12 depends on T11; T13 depends on T12

---

## Code Standards (applies to all code tasks)

### Comments

Every module must have a module-level docstring stating its purpose and the file paths it reads/writes. Every public function must have a one-line docstring. Non-obvious logic (filter graph construction, xfade offset formula, offset tracking, lock protocol) must have an inline comment explaining **why**, not what.

### Logging

All Python modules use the standard `logging` module with a module-level logger (`logger = logging.getLogger(__name__)`). Log levels:

| Level | When to use |
|-------|-------------|
| `DEBUG` | Internal transitions — file reads, gws calls, lock acquired/released |
| `INFO` | Meaningful changes — photo downloaded, video generated, state written |
| `WARNING` | Unexpected-but-handled — empty folder, zero-byte file skipped |
| `ERROR` | Failures that prevent an operation completing |

**Sensitive data must never appear in log output.** Hard rule, no exceptions.

| Field | Classification | Log policy |
|-------|---------------|------------|
| Project name | Admin-chosen folder name | Safe to log |
| Drive folder / file IDs | Internal opaque IDs | Safe to log at DEBUG |
| Drive folder links | URLs | Safe to log at INFO |
| Admin email address | PII | Never log — omit or use `"<redacted>"` |
| Telegram chat ID | PII | Never log |
| Telegram bot token | Secret | Never log |
| Photo filenames | May contain PII | Never log — use count only |
| Error details from FFmpeg or gws | May contain paths | Safe to log — no credentials in these |

Log configuration is the caller's responsibility — modules must not call `logging.basicConfig()` or add handlers.

---

## T01 — `tools/state.py` + unit tests

**What:** Manages `data/photo-agent/state.json`. Stores the pending approval record and Telegram update offset. All read-modify-write operations hold an `fcntl.flock` exclusive lock.

**State file:** `data/photo-agent/state.json`

```json
{
  "telegram_update_offset": 0,
  "pending_approval": null
}
```

When a video is awaiting approval, `pending_approval` holds the full record defined in `techplan.md` (project name, Drive IDs, folder link, local path, Telegram message ID, triggered-at timestamp).

**Functions to implement:**

- `get_pending_approval() -> dict | None` — returns the record or None if null / file missing
- `set_pending_approval(record: dict) -> None` — writes record; rejects if record is missing required keys
- `clear_pending_approval() -> None` — sets `pending_approval` to null
- `get_telegram_offset() -> int` — returns offset; returns 0 if file missing
- `set_telegram_offset(offset: int) -> None` — writes offset

**Tests to write (`tests/test_state.py`):**
- `get_pending_approval()` returns None when state.json does not exist
- `get_pending_approval()` returns None when `pending_approval` is null in the file
- `set_pending_approval()` writes the record; subsequent `get_pending_approval()` returns it
- `clear_pending_approval()` sets `pending_approval` to null; `get_pending_approval()` returns None
- `get_telegram_offset()` returns 0 when state.json does not exist
- `get_telegram_offset()` returns stored value after `set_telegram_offset()`
- `set_telegram_offset()` does not overwrite `pending_approval`
- State file is created if it does not exist
- Concurrent calls from two threads do not corrupt the file (locking test)

**Done when:** `pytest tests/test_state.py` passes.

---

## T02 — `tools/logger.py` + unit tests

**What:** Appends pipe-delimited event lines to `logs/photo-agent.log`. Creates the log directory if it does not exist.

**Log format (from techplan):**
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

**Functions to implement:**
- `log_command(project_name: str) -> None`
- `log_downloaded(project_name: str, count: int) -> None`
- `log_generated(project_name: str, duration_sec: float, size_bytes: int) -> None`
- `log_uploaded(project_name: str, drive_file_id: str) -> None`
- `log_approval_req(project_name: str, message_id: int) -> None`
- `log_approved(project_name: str) -> None`
- `log_rejected(project_name: str) -> None`
- `log_error(project_name: str, phase: str, detail: str) -> None`

**Tests to write (`tests/test_logger.py`):**
- Each function produces a line matching the expected format exactly
- Appends to an existing file (does not overwrite)
- Creates log directory if it does not exist
- Timestamp format is `YYYY-MM-DD HH:MM`
- `log_error` includes both `phase` and `detail` fields

**Done when:** `pytest tests/test_logger.py` passes.

---

## T03 — `tools/video_generator.py` + unit tests

**What:** `VideoGenerator` protocol, `VideoConfig` dataclass, `FFmpegVideoGenerator` implementation, and `VideoGenerationError` exception. FFmpeg is invoked via `subprocess`. Tests mock the subprocess call — no actual video is generated.

**Types to implement:**

```python
@dataclass
class VideoConfig:
    width: int = 1080
    height: int = 1920
    fps: int = 30
    seconds_per_photo: int = 4
    crossfade_duration: float = 0.5
    bitrate: str = "3M"

class VideoGenerationError(Exception):
    pass

class VideoGenerator(Protocol):
    def generate(self, photos: list[Path], config: VideoConfig, output_path: Path) -> Path: ...

class FFmpegVideoGenerator:
    def generate(self, photos: list[Path], config: VideoConfig, output_path: Path) -> Path: ...
```

**FFmpeg command structure (from techplan):**

Per-photo scale/crop filter:
```
[{i}:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={fps}[v{i}]
```

Crossfade chain (N photos → N−1 transitions). Offset formula:
```
offset[i] = (i + 1) × (seconds_per_photo − crossfade_duration)
```

Output flags: `-map [xout] -c:v libx264 -preset medium -b:v {bitrate} -an -r {fps} -pix_fmt yuv420p`

For N=1 (single photo): skip the xfade chain entirely; use `-t {seconds_per_photo}` on the input.

Raises `VideoGenerationError` if FFmpeg exits non-zero, including stderr in the message.

**Tests to write (`tests/test_video_generator.py`):**
- `VideoConfig` defaults match spec values
- For N=1: FFmpeg command does not include `xfade`; includes `-t {seconds_per_photo}`
- For N=2: one `xfade` filter with offset = `seconds_per_photo − crossfade_duration`
- For N=5: four `xfade` filters; each offset matches the formula
- Scale/crop filter string is correct for the default resolution
- Output flags include `-c:v libx264`, `-pix_fmt yuv420p`, `-an`
- `generate()` returns the `output_path` passed in
- `generate()` raises `VideoGenerationError` when FFmpeg exits non-zero
- `generate()` includes FFmpeg stderr in the `VideoGenerationError` message

**Done when:** `pytest tests/test_video_generator.py` passes.

---

## T04 — `tools/drive.py` + unit tests

**What:** Thin wrapper around `gws drive` subcommands. Each function builds a `gws` CLI call via `subprocess`, parses the JSON output, and returns a clean Python value. Raises `RuntimeError` on non-zero gws exit.

**Custom exception:** `DriveFolderNotFoundError(RuntimeError)` — raised by `find_folder()` when no matching folder exists.

**Functions to implement (from techplan):**

- `find_folder(name: str, parent_id: str) -> str` — queries by name and parent, returns folder ID; raises `DriveFolderNotFoundError` if absent
- `list_photos(folder_id: str) -> list[dict]` — returns `[{"id": ..., "name": ...}]`; filtered to `image/jpeg` and `image/png` MIME types; sorted alphabetically by name; zero-byte files skipped
- `download(file_id: str, output_path: Path) -> None` — runs `gws drive files get --fileId {id} --output {path}`
- `upload(local_path: Path, parent_id: str, name: str) -> str` — runs `gws drive +upload {path} --parent {id} --name {name}`; returns Drive file ID from response
- `delete(file_id: str) -> None` — runs `gws drive files delete --fileId {id}`
- `folder_link(folder_id: str) -> str` — returns `https://drive.google.com/drive/folders/{folder_id}`

**Tests to write (`tests/test_drive.py`):**
- `find_folder()` parses gws JSON and returns the matching folder ID
- `find_folder()` raises `DriveFolderNotFoundError` when gws returns an empty file list
- `list_photos()` filters out non-image MIME types
- `list_photos()` sorts results alphabetically by `name`
- `list_photos()` skips files with zero size
- `download()` passes `--output` flag with the correct path
- `upload()` passes `--parent` and `--name` flags; returns file ID from parsed response
- `delete()` passes correct `--fileId`
- `folder_link()` returns the correct URL string
- Non-zero gws exit raises `RuntimeError` for all functions

**Done when:** `pytest tests/test_drive.py` passes.

---

## T05 — `tools/telegram_api.py` + unit tests

**What:** Direct HTTP calls to the Telegram Bot API using `requests`. Used for inline keyboard messages (`sendMessage` with `reply_markup`), callback dismissal (`answerCallbackQuery`), and update polling (`getUpdates`). Bot token is read from `TELEGRAM_BOT_TOKEN` in the environment.

**Functions to implement:**

- `send_message_with_buttons(chat_id: str, text: str, buttons: list[tuple[str, str]]) -> int`
  — `buttons` is a list of `(label, callback_data)` pairs; sends `sendMessage` with inline keyboard; returns Telegram `message_id`
- `answer_callback_query(callback_query_id: str) -> None`
  — calls `answerCallbackQuery` to dismiss the spinner on the admin's button tap
- `get_updates(offset: int) -> list[dict]`
  — calls `getUpdates?offset={offset}&timeout=0`; returns the raw list of update objects (empty list if none)

All functions raise `RuntimeError` on HTTP error or non-OK Telegram response.

**Tests to write (`tests/test_telegram_api.py`):**
- `send_message_with_buttons()` constructs `reply_markup` JSON with the correct inline keyboard structure
- `send_message_with_buttons()` extracts and returns `message_id` from the response
- `answer_callback_query()` calls the correct endpoint with the callback query ID
- `get_updates()` passes the offset as a query parameter
- `get_updates()` returns an empty list when the response contains no updates
- Non-OK HTTP status raises `RuntimeError`
- Non-OK Telegram `ok: false` response raises `RuntimeError`
- Bot token is read from `TELEGRAM_BOT_TOKEN` env var (test sets it in the environment)

**Done when:** `pytest tests/test_telegram_api.py` passes.

---

## T06 — `.env.example` + `requirements.txt`

**What:** Config template and dependency list. Documents every required variable. `requirements.txt` lists all runtime and test dependencies.

**`.env.example` variables:**

| Variable | Description | Example |
|----------|-------------|---------|
| `AGENT_EMAIL` | Gmail address used to send the approval email | `fieldkit.agent@gmail.com` |
| `ADMIN_EMAIL` | Recipient of the approval email | `admin@mybusiness.com` |
| `ADMIN_TELEGRAM_CHAT_ID` | Admin's Telegram chat ID | `123456789` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token — required; OpenClaw does not expose this for script use | |
| `DRIVE_ROOT_FOLDER_ID` | Google Drive ID of the root project folder | |
| `SECONDS_PER_PHOTO` | Display duration per photo in seconds | `4` |
| `VIDEO_TMP_DIR` | Local temp directory for photos and generated video | `data/photo-agent/tmp` |

**`requirements.txt`:**
```
pytest
pytest-mock
requests
```

**Done when:** File exists with all variables documented, comment at top explains it must be copied to `.env` and never committed, and `requirements.txt` is complete.

---

## T07 — `scripts/process_photos.py` + unit tests

**What:** The main processing script. Invoked by the `/process_photos` OpenClaw skill with `--project <name>`. Integrates tools from T01–T05. Implements the full data flow from `techplan.md` Section A. External calls (gws, FFmpeg, Telegram API, Drive) are mocked in tests.

**Also implements:** the `scrub(photos: list[Path]) -> list[Path]` no-op placeholder function (returns input unchanged) in a `pipeline.py` module or inline in the script. The function must exist as a named, callable step in the pipeline so it can be activated later.

**CLI interface:**
```bash
python3 scripts/process_photos.py --project <name>
```

**Tests to write (`tests/test_process_photos.py`):**
- Missing `--project` arg → `openclaw message send` Telegram error; exits non-zero
- Existing `pending_approval` in state → Telegram error "already awaiting approval"; exits
- Drive folder not found (`DriveFolderNotFoundError`) → Telegram error; exits
- Fewer than 2 photos in folder → Telegram error with count; exits
- More than 30 photos in folder → Telegram error; exits
- Photo download failure → Telegram error; remaining downloads aborted; temp dir cleaned; exits
- FFmpeg failure (`VideoGenerationError`) → Telegram error with reason; exits
- Drive upload failure → Telegram error; local file retained (not deleted); exits
- Happy path: tools called in correct sequence (discover → download → scrub → generate → upload → send buttons → write state)
- Happy path: `state.set_pending_approval()` called with all required fields
- Happy path: `telegram_api.send_message_with_buttons()` called with ✅/❌ button labels
- Happy path: temp directory cleared and recreated before each run for the same project
- Happy path: approval message text includes project name, photo count, and duration

**Done when:** `pytest tests/test_process_photos.py` passes.

---

## T08 — `scripts/check_approval.py` + unit tests

**What:** The approval polling script. Invoked by the 1-minute cron and the `/check_approval` on-demand command. Reads pending approval state, polls Telegram `getUpdates`, and dispatches approve or reject logic. Exits immediately if no approval is pending. External calls mocked in tests.

**CLI interface:**
```bash
python3 scripts/check_approval.py           # on-demand (/check_approval)
python3 scripts/check_approval.py --source cron  # cron — silent on no pending approval
```

**Tests to write (`tests/test_check_approval.py`):**
- No `pending_approval` in state → exits immediately; no Telegram or Drive calls made
- Updates contain no matching `callback_query` → offset updated in state; exits
- ✅ Approve callback: `answer_callback_query()` called first; approval email sent via gws; Telegram confirmation sent; local file deleted; state cleared; offset updated
- ❌ Reject callback: `answer_callback_query()` called first; Drive file deleted; local file deleted; Telegram rejection message sent; state cleared; offset updated
- Telegram offset set to `max(update_id) + 1` after each run
- Email send failure on approve → Telegram fallback message includes Drive folder link; state still cleared
- Drive delete failure on reject → logged; Telegram rejection still sent; state cleared (Drive garbage-collection is best-effort)
- `--source cron` flag: no output on "nothing pending" (silent exit)

**Done when:** `pytest tests/test_check_approval.py` passes.

---

## T09 — `SKILL_process_photos.md` + `SKILL_check_approval.md`

**What:** Two OpenClaw skill definition files. Content is fully specified in the techplan (Component 1 and Component 2). Install both skills in the OpenClaw skill directory on the Mac Mini.

**`SKILL_process_photos.md` frontmatter:**
```yaml
name: process_photos
description: Generate a video from photos in a Google Drive project folder and send it to the admin for approval
user-invocable: true
metadata: {"openclaw": {"requires": {"bins": ["gws", "python3", "ffmpeg"]}}}
```

**`SKILL_check_approval.md` frontmatter:**
```yaml
name: check_approval
description: Check for a pending video approval response from the admin and process it
user-invocable: true
metadata: {"openclaw": {"requires": {"bins": ["python3"]}}}
```

Both skill bodies instruct the agent to run the corresponding Python script and report its output. Neither skill body should improvise, call Drive, or interpret Telegram updates itself.

**Done when:** `openclaw skills list` shows both `process_photos` and `check_approval` with no load errors.

---

## T10 — Mac Mini environment setup

**What:** One-time environment setup. Follow the checklist from `techplan.md` exactly.

**Checklist:**
- [ ] OpenClaw installed and running (existing from Feature 001)
- [ ] OpenClaw Telegram channel configured (existing from Feature 001)
- [ ] `gws` re-authenticated with Drive scope: `gws auth login --account $AGENT_EMAIL --scopes drive`
- [ ] Verify gws Drive access: `gws drive files list --params '{"pageSize": 1}'` returns valid JSON
- [ ] `ffmpeg` installed: `brew install ffmpeg`; verify: `ffmpeg -version`
- [ ] `pip install -r requirements.txt` from the photo-agent directory
- [ ] Runtime directories created: `mkdir -p ~/src/fieldkit/data/photo-agent/tmp ~/src/fieldkit/logs`
- [ ] `.env` created from `.env.example` and populated with all variables
- [ ] Admin creates the Drive root folder, notes its ID, sets `DRIVE_ROOT_FOLDER_ID` in `.env`
- [ ] `SKILL_process_photos.md` installed in OpenClaw skill directory
- [ ] `SKILL_check_approval.md` installed in OpenClaw skill directory
- [ ] Approval cron registered (heredoc command from techplan Component 3)
- [ ] Verify cron is registered: `crontab -l` shows the check_approval entry

**Done when:** All checklist items checked off. `gws drive files list --params '{"pageSize": 1}'` returns a valid response.

---

## T11 — Smoke test: `/process_photos` manual trigger

**What:** First end-to-end test. Verifies the full processing pipeline before testing the approval loop.

**Steps:**
1. In Google Drive, create a subfolder named `fk-smoke-test` under the root folder
2. Upload 3 photos to it — name them `01_test.jpg`, `02_test.jpg`, `03_test.jpg`
3. Send `/process_photos fk-smoke-test` via Telegram
4. Verify Telegram approval message arrives with project name, photo count (3), and duration (10 sec at 4 sec/photo − 2 × 0.5 sec xfade = 10 sec)
5. Verify the message includes both ✅ Approve and ❌ Reject inline buttons
6. Verify a `.mp4` file appears in the `fk-smoke-test` Drive folder
7. Verify `data/photo-agent/state.json` has a `pending_approval` record with correct project name and Drive file ID
8. Verify `logs/photo-agent.log` has lines: `COMMAND`, `DOWNLOADED count=3`, `GENERATED`, `UPLOADED`, `APPROVAL_REQ`

**Also verify error paths:**
9. While approval is pending, send `/process_photos fk-smoke-test` again
10. Verify Telegram error: "already awaiting approval"
11. Resolve the pending approval (tap ✅ or ❌) before proceeding to T12

**Done when:** All 11 steps pass.

---

## T12 — Smoke test: approval flow (approve + reject paths)

**What:** Verifies both sides of the approval dialog.

**Approve path:**
1. Send `/process_photos fk-smoke-test` via Telegram
2. Tap ✅ Approve (or send `/check_approval` if cron is not yet registered)
3. Verify approval email received at `ADMIN_EMAIL` with subject containing project name and a Drive folder link
4. Verify Telegram confirmation message sent
5. Verify local temp file deleted (`data/photo-agent/tmp/fk-smoke-test/` is empty or removed)
6. Verify `state.json` `pending_approval` is null
7. Verify `photo-agent.log` has `APPROVED` line

**Reject path:**
8. Send `/process_photos fk-smoke-test` via Telegram (new video generated)
9. Tap ❌ Reject (or send `/check_approval`)
10. Verify the generated `.mp4` is removed from the Drive `fk-smoke-test` folder
11. Verify Telegram rejection message sent
12. Verify local temp file deleted
13. Verify `state.json` `pending_approval` is null
14. Verify `photo-agent.log` has `REJECTED` line

**Done when:** All 14 steps pass.

---

## T13 — Smoke test: approval cron

**What:** Verifies the 1-minute approval cron processes callbacks without manual `/check_approval` trigger.

**Steps:**
1. Send `/process_photos fk-smoke-test` via Telegram
2. Wait for the approval message to arrive in Telegram
3. Tap ✅ Approve — do **not** send `/check_approval`
4. Wait up to 2 minutes
5. Verify approval email arrives at `ADMIN_EMAIL`
6. Verify Telegram confirmation message arrives
7. Verify `logs/cron.log` shows recent `check_approval.py` entries

**Done when:** All 7 steps pass. Feature 002 is live.

---

## Summary

| Task | Depends on | Type | Done when |
|------|-----------|------|-----------|
| T01 — `state.py` + tests | — | Code | `pytest tests/test_state.py` passes |
| T02 — `logger.py` + tests | — | Code | `pytest tests/test_logger.py` passes |
| T03 — `video_generator.py` + tests | — | Code | `pytest tests/test_video_generator.py` passes |
| T04 — `drive.py` + tests | — | Code | `pytest tests/test_drive.py` passes |
| T05 — `telegram_api.py` + tests | — | Code | `pytest tests/test_telegram_api.py` passes |
| T06 — `.env.example` + `requirements.txt` | — | Config | All variables present, verified manually |
| **── gate: `pytest tests/` clean across T01–T05 ──** | | | |
| T07 — `process_photos.py` + tests | T01–T05 | Code | `pytest tests/test_process_photos.py` passes |
| T08 — `check_approval.py` + tests | T01, T02, T04, T05 | Code | `pytest tests/test_check_approval.py` passes |
| **── gate: `pytest tests/` clean across T07–T08 ──** | | | |
| T09 — SKILL files | T07, T08 | Skill | Both skills appear in `openclaw skills list` |
| **── gate: T09 verified in OpenClaw ──** | | | |
| T10 — Mac Mini setup | T06, T09 | Setup | All checklist items checked; gws Drive verified |
| **── gate: T10 checklist fully checked ──** | | | |
| T11 — Smoke test: `/process_photos` | T10 | Test | All 11 steps pass |
| T12 — Smoke test: approval flow | T11 | Test | All 14 steps pass |
| T13 — Smoke test: approval cron | T12 | Test | All 7 steps pass — Feature 002 is live |
