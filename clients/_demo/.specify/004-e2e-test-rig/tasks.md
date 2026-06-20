# Tasks: End-to-End Test Rig (Feature 004)

**Input**: Design documents from `clients/_demo/.specify/004-e2e-test-rig/`
**Prerequisites**: plan.md ✅, spec.md ✅, research.md ✅, data-model.md ✅
**Testing**: TDD — write tests first, verify they FAIL, then implement until they PASS

**Organization**: Grouped by user story (US1 P1 → US2 P2 → US3 P3)

---

## Phase 1: Setup

**Purpose**: Create Gherkin acceptance test files and project scaffolding.

- [x] T001 Create Gherkin feature files in `clients/_demo/.specify/004-e2e-test-rig/features/`: `pipeline_test.feature` (US1+US2) and `cleanup.feature` (US3) — content defined in this feature directory

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Bug fix (local file lifecycle) and new tool functions that the main script depends on. All four groups are independent and can run in parallel across groups, but within each group the test task precedes the implementation task.

**⚠️ CRITICAL**: No US1/US2/US3 work can begin until this phase is complete and `pytest` is green.

### Group A — Fix `check_approval.py` local file bug

- [x] T002 [P] Write test in `clients/_demo/src/photo-agent/tests/test_check_approval.py` asserting `_delete_local_file` is NOT called when callback_data is `approve`
- [x] T003 [P] Remove the `_delete_local_file(video_local_path, project_name)` call from the `approve` branch in `clients/_demo/src/photo-agent/scripts/check_approval.py` (line ~389) — verify T002 now passes

### Group B — Fix `upload_facebook.py` local file lifecycle

- [x] T004 [P] Write test in `clients/_demo/src/photo-agent/tests/test_upload_facebook.py` asserting that the local video file is deleted after `facebook_state.mark_published()` on the success path
- [x] T005 [P] Add `_delete_local_file(video_path, project_name)` call after `facebook_state.mark_published(...)` in `clients/_demo/src/photo-agent/scripts/upload_facebook.py` success path — copy the safety logic (allowed-root check) from `check_approval.py`; best-effort (log on failure, do not raise) — verify T004 now passes

### Group C — Extend `drive.py`

- [x] T006 [P] Write tests in `clients/_demo/src/photo-agent/tests/test_drive.py` for: `create_folder()` happy path, HTTP error, unsafe name; and `upload()` with `content_type="image/jpeg"`
- [x] T007 [P] Add `create_folder(name: str, parent_id: str) -> str` to `clients/_demo/src/photo-agent/tools/drive.py`: validate name against `_SAFE_FOLDER_NAME_RE`, POST to Drive v3 files API with `mimeType=application/vnd.google-apps.folder`, return the `id` field, raise `RuntimeError` on HTTP error
- [x] T008 [P] Add optional `content_type: str = "video/mp4"` parameter to `drive.upload()` in `clients/_demo/src/photo-agent/tools/drive.py` — use it in the `X-Upload-Content-Type` header (default unchanged; backward compatible) — verify T006 now passes

### Group D — Extend `facebook_api.py`

- [x] T009 [P] Write tests in `clients/_demo/src/photo-agent/tests/test_facebook_api.py` for `delete_post()`: success (HTTP 200, `{"success": true}`), Graph API error code 100 ("does not exist") raises `FacebookUploadError`
- [x] T010 [P] Add `delete_post(page_access_token: str, post_id: str) -> None` to `clients/_demo/src/photo-agent/tools/facebook_api.py`: `DELETE /v25.0/{post_id}?access_token=...`, raise `FacebookUploadError` on any error including code 100 — callers handle 100 as a warning — verify T009 now passes

### Checkpoint

- [x] T011 Run `pytest clients/_demo/src/photo-agent/tests/` — all tests green, no regressions

---

## Phase 3: User Story 1 — Run End-to-End Pipeline Test (Priority: P1) 🎯 MVP

**Goal**: A single `python3 scripts/run_e2e_test.py` command exercises the full pipeline from JPEG clock frame generation through Facebook post live, reporting ✅/❌ per stage.

**Independent Test**: Run the script with valid `.env`; observe all five stages complete and a clock-face video appears on the Facebook demo page.

> **NOTE: Write test stubs FIRST (T012–T016) and verify they FAIL before implementing T017–T024.**

### Tests for User Story 1

- [x] T012 [P] [US1] Write test for pre-flight checks in `clients/_demo/src/photo-agent/tests/test_run_e2e_test.py`: missing env vars exit with non-zero, pending approval exits with clear error, FFmpeg absent exits with clear error, `--duration` out of range [2, 300] exits with clear error
- [x] T013 [P] [US1] Write test for Stage 1 in `clients/_demo/src/photo-agent/tests/test_run_e2e_test.py`: mock `subprocess.run` (FFmpeg); verify `n_frames` calculation from `--duration` and `SECONDS_PER_PHOTO`; verify `spp_effective` bumped correctly when `n_frames > 30`; verify output JPEG count matches `n_frames`
- [x] T014 [P] [US1] Write test for Stage 2 in `clients/_demo/src/photo-agent/tests/test_run_e2e_test.py`: mock `drive.create_folder()` and `drive.upload()`; verify `create_folder` called once with test project name; verify `upload` called `n_frames` times with `content_type="image/jpeg"`
- [x] T015 [P] [US1] Write test for Stage 3 in `clients/_demo/src/photo-agent/tests/test_run_e2e_test.py`: mock `subprocess.run` (process_photos) and `state.get_pending_approval()`; verify subprocess called with correct `--project` arg and `SECONDS_PER_PHOTO` env override; verify polling detects pending approval
- [x] T016 [P] [US1] Write tests for Stages 4–5 in `clients/_demo/src/photo-agent/tests/test_run_e2e_test.py`: mock `state.get_pending_approval()` returning None + `facebook_state.get_pending_upload()` returning test project record (Stage 4); mock `facebook_state.get_pending_upload()` returning `status="published"` (Stage 5); verify timeout exits non-zero

### Implementation for User Story 1

- [x] T017 [US1] Create `clients/_demo/src/photo-agent/scripts/run_e2e_test.py`: argument parser (`--duration`, `--timeout`, `--approval-timeout`, `--cleanup`, `--dry-run`), `.env` loading via `load_dotenv`, `_compute_frames(duration, spp_base) -> (n_frames, spp_effective)` helper
- [x] T018 [US1] Implement pre-flight checks in `scripts/run_e2e_test.py`: verify required env vars (`FB_PAGE_ACCESS_TOKEN`, `FB_PAGE_ID`, `DRIVE_ROOT_FOLDER_ID`, `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_CHAT_ID`), `state.get_pending_approval() is None`, `shutil.which("ffmpeg")` not None, `--duration` in [2, 300]
- [x] T019 [US1] Implement Stage 1 `_generate_clock_frames(n_frames, start_unix, frames_dir)` in `scripts/run_e2e_test.py`: single FFmpeg subprocess with `rate=1`, `drawtext` using `%{pts\:localtime\:START_UNIX\:%m/%d/%Y}` (date line) and `%{pts\:localtime\:START_UNIX\:%H\:%M\:%S}` (time line), `-frames:v n_frames`, output to `frame_%03d.jpg`; verify all files exist and are non-zero
- [x] T020 [US1] Implement Stage 2 `_upload_frames_to_drive(frames_dir, test_name) -> (folder_id, file_ids)` in `scripts/run_e2e_test.py`: `drive.create_folder(test_name, root_folder_id)`, then `drive.upload(frame, folder_id, frame.name, content_type="image/jpeg")` for each frame in sorted order
- [x] T021 [US1] Implement Stage 3 `_run_process_photos(test_name, spp_effective, timeout)` in `scripts/run_e2e_test.py`: `subprocess.run([sys.executable, "clients/_demo/src/photo-agent/scripts/process_photos.py", "--project", test_name], env={...os.environ, "SECONDS_PER_PHOTO": str(spp_effective)}, check=True, timeout=timeout)`; then poll `state.get_pending_approval()` until `record["project_name"] == test_name` or stage timeout
- [x] T022 [US1] Implement Stage 4 `_wait_for_approval(test_name, timeout)` in `scripts/run_e2e_test.py`: print "Tap Approve in Telegram"; poll every 10 s until `state.get_pending_approval() is None` AND `facebook_state.get_pending_upload()["project_name"] == test_name`; exit non-zero on timeout
- [x] T023 [US1] Implement Stage 5 `_wait_for_facebook_post(test_name, timeout)` in `scripts/run_e2e_test.py`: poll every 10 s until `facebook_state.get_pending_upload()["status"] == "published"`; return `fb_post_id` on success; exit non-zero on timeout
- [x] T024 [US1] Wire all stages into `main()` in `scripts/run_e2e_test.py`; run `pytest tests/test_run_e2e_test.py` — T012–T016 must all pass

**Checkpoint**: `python3 scripts/run_e2e_test.py --dry-run` exits 0 (env check only). All US1 tests green.

---

## Phase 4: User Story 2 — Observe Real-Time Stage Progress (Priority: P2)

**Goal**: Every stage transition prints a timestamped `[HH:MM:SS] Stage N/5: … ✅ done (Xs)` line to stdout as it happens. Timeouts print a clear message before exiting.

**Independent Test**: Run the script; observe each stage line appears within 5 s of the stage completing, with elapsed time shown.

> **NOTE: US2 enhances the same `run_e2e_test.py` written in US1. Confirm US1 checkpoint passes before starting US2.**

### Tests for User Story 2

- [x] T025 [US2] Write test in `clients/_demo/src/photo-agent/tests/test_run_e2e_test.py` for stdout output format: verify `[HH:MM:SS]` prefix, `Stage N/5:` label, `✅ done (Xs)` suffix on success, and `❌ failed` / timeout message format on failure

### Implementation for User Story 2

- [x] T026 [US2] Add `_print_stage(n, total, label, status, elapsed)` helper and wrap each stage call in `scripts/run_e2e_test.py` to capture start time, call the stage, compute elapsed, and print the stage line
- [x] T027 [US2] Add per-timeout message in `scripts/run_e2e_test.py`: when any stage poll exceeds its timeout, print `[HH:MM:SS] Stage N/5: {label} ❌ timed out after {elapsed}s` before `sys.exit(1)`

**Checkpoint**: Live run shows timestamped stage lines appearing as each stage completes.

---

## Phase 5: User Story 3 — Clean Up Test Artifacts (Priority: P3)

**Goal**: `python3 scripts/run_e2e_test.py --cleanup` deletes the Drive test folder and the Facebook test post created by the most recent run.

**Independent Test**: Run the script with `--cleanup` after a successful test; verify Drive folder gone and Facebook post deleted. Run again with `--cleanup` after manually deleting the post; verify script logs a warning and exits 0.

> **NOTE: US3 depends on `drive.delete()` (already in drive.py) and `facebook_api.delete_post()` (added in T010).**

### Tests for User Story 3

- [x] T028 [US3] Write tests for `--cleanup` in `clients/_demo/src/photo-agent/tests/test_run_e2e_test.py`: mock `drive.delete()` and `facebook_api.delete_post()`; verify both called with correct IDs on success; verify `FacebookUploadError` with code 100 is caught and logged as warning (not exception); verify exit code 0 on both paths

### Implementation for User Story 3

- [x] T029 [US3] Add `_cleanup(folder_id: str, fb_post_id: str | None, page_token: str)` to `scripts/run_e2e_test.py`
- [x] T030 [US3] Implement Drive folder deletion in `_cleanup()` in `scripts/run_e2e_test.py`: `drive.delete(folder_id)` — log error on failure but do not raise
- [x] T031 [US3] Implement Facebook post deletion in `_cleanup()` in `scripts/run_e2e_test.py`: `facebook_api.delete_post(page_token, fb_post_id)` in try/except; catch `FacebookUploadError`, log `WARNING: Facebook post {fb_post_id} not found or already deleted — skipping`, continue
- [x] T032 [US3] Wire `--cleanup` into `main()` in `scripts/run_e2e_test.py`: if `--cleanup` flag is set after a successful run (or passed standalone with stored IDs), call `_cleanup()`; run T028 tests — all must pass

**Checkpoint**: `python3 scripts/run_e2e_test.py --cleanup` removes Drive folder and Facebook post. Second call with post already deleted exits 0 with a warning.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [x] T033 Run full pytest suite: `pytest clients/_demo/src/photo-agent/tests/` — all tests green, zero regressions vs Feature 003 baseline
- [ ] T034 Run live end-to-end test: `python3 scripts/run_e2e_test.py --duration 30` — observe all five stages pass and clock-face video appears on the Facebook demo page
- [ ] T035 Run adversarial review per project standards; fix any Critical or High findings before merging
- [ ] T036 Update `CLAUDE.md` active feature reference to mark Feature 004 complete and set Feature 005 (Instagram Video Upload) as next

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1 — BLOCKS all user stories
- **Phase 3 (US1)**: Depends on Phase 2 complete and T011 green
- **Phase 4 (US2)**: Depends on Phase 3 checkpoint
- **Phase 5 (US3)**: Depends on T010 (`facebook_api.delete_post`) from Phase 2 and Phase 3 checkpoint
- **Phase 6 (Polish)**: Depends on all user story phases complete

### User Story Dependencies

- **US1 (P1)**: Depends on all Phase 2 groups (A, B, C, D)
- **US2 (P2)**: Depends on US1 checkpoint — enhances the same script
- **US3 (P3)**: Depends on US1 checkpoint + T010 (delete_post) — adds cleanup to the same script

### Within Each Group (Phase 2)

- Tests MUST be written and confirmed failing before implementation
- Group A (T002→T003), Group B (T004→T005), Group C (T006→T007→T008), Group D (T009→T010) are all sequential within group
- Groups A, B, C, D are fully independent — run in parallel across groups

---

## Parallel Opportunities

### Phase 2 — across groups simultaneously

```
Group A: T002 → T003
Group B: T004 → T005   ← in parallel with Group A
Group C: T006 → T007 → T008   ← in parallel with A and B
Group D: T009 → T010   ← in parallel with A, B, C
```

### Phase 3 — test stubs in parallel before implementation

```
Write all test stubs in parallel (all touch test_run_e2e_test.py — do sequentially or in one sitting):
T012, T013, T014, T015, T016

Then implement sequentially (all touch run_e2e_test.py):
T017 → T018 → T019 → T020 → T021 → T022 → T023 → T024
```

---

## Implementation Strategy

### MVP (US1 only — ~3 hours)

1. Phase 1: T001 (Gherkin files)
2. Phase 2: All groups in parallel → T011 green
3. Phase 3: T012–T024 → US1 checkpoint

**STOP and VALIDATE**: Run the live e2e test. Confirm clock video appears on Facebook.

### Full Feature

4. Phase 4: T025–T027 (US2 progress output)
5. Phase 5: T028–T032 (US3 cleanup)
6. Phase 6: T033–T036 (polish + review)

---

## Notes

- `[P]` = can run in parallel with other `[P]` tasks in the same phase (different files, no dependency)
- `[USn]` = maps to user story n in spec.md
- TDD discipline: run `pytest` after each test stub to confirm it FAILS, then after implementation to confirm it PASSES
- `spp_effective` override is passed only to the `process_photos.py` subprocess env — never written to `.env`
- The local video file bug (T002–T005) MUST be fixed before any live test — otherwise Stage 5 will always fail
