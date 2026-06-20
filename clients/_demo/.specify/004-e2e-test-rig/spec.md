# Feature Specification: End-to-End Test Rig

**Feature Branch**: `004-e2e-test-rig`

**Created**: 2026-06-20

**Status**: Draft

**Input**: End-to-end test rig: generates a test video with a timestamp, uploads it to Google Drive in the correct project folder structure, then triggers the full photo-agent workflow — process_photos.py generates the video, check_approval.py sends the Telegram approval request, owner approves, upload_facebook.py posts to the Facebook Page. Goal is a single script that exercises the entire pipeline from Drive upload through Facebook post with no manual setup steps beyond a one-time config.

---

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Run End-to-End Pipeline Test (Priority: P1)

The admin runs a single script that seeds Google Drive with synthetic test content, waits for each pipeline stage to complete, and reports pass/fail at every step. The test ends when the video is live on the Facebook Page and a Telegram confirmation has been received — or when any stage fails with a clear error message.

**Why this priority**: The core purpose of this feature. Without this, there is no automated way to verify the full pipeline is working end-to-end after changes or credential rotations.

**Independent Test**: Run the script against a real (or staging) environment and observe that all five pipeline stages complete without manual intervention beyond the Telegram approval tap.

**Acceptance Scenarios**:

1. **Given** the script is run with a valid config, **When** all env vars and credentials are in place, **Then** it generates a ticking-clock test video, uploads it to Google Drive, sends a Telegram approval request, receives the admin's approval, and posts the video to the Facebook Page — reporting ✅ at each stage.

2. **Given** a pipeline stage fails (e.g., Drive upload fails), **When** the error occurs, **Then** the script reports ❌ for that stage with a clear error message and exits without continuing to the next stage.

3. **Given** the script has completed successfully, **When** the admin checks Facebook, **Then** a post with the test video is visible on the Fieldkit demo page — the video shows the date and time in MM/DD/YYYY HH:MM:SS format ticking in real-time for the full duration, making it immediately identifiable as a test post.

---

### User Story 2 — Observe Real-Time Stage Progress (Priority: P2)

The admin can see which pipeline stage is currently active as the test runs, rather than waiting in silence and only seeing results at the end.

**Why this priority**: The pipeline spans multiple cron cycles (each 1 minute apart). Without progress output the admin cannot tell if the test is running or stalled.

**Independent Test**: Run the script and verify that each stage transition prints a timestamped status line to stdout as it happens, including how long each stage took.

**Acceptance Scenarios**:

1. **Given** the script is running, **When** each stage starts or completes, **Then** a timestamped line is printed to stdout (e.g., `[14:05:01] Stage 2/5: Waiting for video generation… ✅ done (38s)`).

2. **Given** a stage is taking longer than expected, **When** the configured timeout is reached, **Then** the script prints a timeout message and exits with a non-zero code.

---

### User Story 3 — Clean Up Test Artifacts (Priority: P3)

After a successful test run the admin can optionally remove the synthetic content from Google Drive and the test post from the Facebook Page, leaving the environment clean for the next test.

**Why this priority**: Without cleanup, repeated test runs accumulate Drive clutter and Facebook posts. Lower priority because the test succeeds regardless of cleanup.

**Independent Test**: Run the script with a `--cleanup` flag after a successful test run and verify that the Drive test folder and Facebook test post are removed.

**Acceptance Scenarios**:

1. **Given** a successful run completed, **When** the script is run again with `--cleanup`, **Then** the Google Drive test folder and the Facebook test post are deleted.

2. **Given** cleanup is requested but the Facebook post was already deleted manually, **When** the delete call fails with "not found", **Then** the script logs a warning and continues without error.

---

### Edge Cases

- What happens if the cron jobs are not running (process_photos.py never fires)?
- What if `check_approval.py` picks up a real pending approval during the test instead of the test one?
- What if the Drive upload succeeds but the folder structure is wrong and process_photos.py ignores it?
- What if the test is run while a previous test's artifacts are still pending?
- What if the Telegram approval message is dismissed or the bot token changes mid-test?

---

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The test rig MUST be invocable as a single command from the repo root with no arguments beyond optional flags.
- **FR-002**: The test rig MUST generate synthetic JPEG image frames showing the current date and time in MM/DD/YYYY HH:MM:SS format (advancing one second per frame), upload them to Google Drive in the folder structure `process_photos.py` expects, and trigger `process_photos.py` to assemble them into a video — so the resulting Facebook post shows a clock face for the entire video duration.
- **FR-003**: The test rig MUST poll for each pipeline stage to complete before moving to the next, using the same state files that the production scripts write (`state.json`, `facebook_state.json`).
- **FR-004**: The test rig MUST emit a clear pass/fail status line for each of the five pipeline stages: Drive upload, video generation, Telegram approval sent, approval received, Facebook post live.
- **FR-005**: The test rig MUST time out each stage after a configurable wait period (default: 3 minutes per stage; default: 10 minutes for the approval stage) and exit with a non-zero code if any stage times out.
- **FR-006**: The test rig MUST use a namespaced project name (e.g., `e2e-test-YYYYMMDD-HHMMSS`) so its state does not collide with real pending approvals. The video duration MUST be at least 2 seconds and at most 5 minutes (300 seconds); the script MUST reject out-of-range values with a clear error.
- **FR-007**: The test rig MUST read all credentials from the existing `.env` file — no separate config file is needed for a one-time setup beyond the credentials already required for the live pipeline.
- **FR-008**: The test rig MUST support a `--cleanup` flag that deletes the Drive test folder and the resulting Facebook post after a successful run.
- **FR-009**: The test rig MUST exit 0 on full success and non-zero on any stage failure or timeout, so it can be used in a manual CI check.

### Key Entities

- **Test Run**: A single execution identified by a timestamp-based project name; owns a Drive folder, a state file entry, and optionally a Facebook post.
- **Pipeline Stage**: One of five checkpoints (Drive upload → video generated → Telegram sent → approved → Facebook post live), each with a pass/fail result and elapsed time.
- **Synthetic Content**: Timestamped image frames generated locally and uploaded to Drive; must be valid input for `process_photos.py`.

---

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: The admin can verify the full pipeline is working end-to-end in under 10 minutes from running the script.
- **SC-002**: Each stage reports pass/fail within 5 seconds of the actual stage completing.
- **SC-003**: The script produces zero false positives — it only reports success when a real Facebook post is verifiably live.
- **SC-004**: Running the script twice back-to-back leaves no orphaned state that causes the second run to fail.
- **SC-005**: A failing stage is reported with enough context (stage name, error message, elapsed time) for the admin to diagnose the issue without reading log files.

---

## Assumptions

- The cron jobs for `check_approval.py` and `upload_facebook.py` are already running on the machine during the test.
- The Telegram approval step requires a manual tap by the admin (the test rig does not auto-approve — this keeps the test representative of real usage and avoids automating the human-in-the-loop gate).
- Synthetic content is a set of JPEG frames generated by FFmpeg (one per second of the clock face), each showing MM/DD/YYYY HH:MM:SS advancing by one second. The number of frames and the `SECONDS_PER_PHOTO` value are derived from `--duration` so the assembled video is approximately the requested length.
- Video duration is configurable via `--duration` (default: 30 seconds, min: 2 seconds, max: 300 seconds / 5 minutes).
- The test always runs against the live Facebook Page and live Telegram bot (no separate staging environment).
- Cleanup of the Facebook test post uses the Graph API with the existing `FB_PAGE_ACCESS_TOKEN`.
- The Drive folder structure expected by `process_photos.py` is: `{DRIVE_ROOT_FOLDER_ID}/{project_name}/` containing JPEG image files.
