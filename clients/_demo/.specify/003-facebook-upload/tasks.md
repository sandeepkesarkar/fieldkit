# Tasks: Feature 003 — Facebook Video Upload

**Feature dir**: `clients/_demo/.specify/003-facebook-upload/`
**Implementation root**: `clients/_demo/src/photo-agent/`
**Branch**: `001-upload-facebook-video`

**Prerequisites**: plan.md ✅, spec.md ✅, research.md ✅, data-model.md ✅, contracts/cli-contracts.md ✅

**TDD required**: Constitution Gate 5 — tests written before or alongside implementation; a feature is NOT complete until all tests pass.

## Format: `[ID] [P?] [Story?] Description`

- **[P]**: Can run in parallel (different files, no shared state)
- **[Story]**: User story this task belongs to (US1/US2/US3)
- Paths are relative to `clients/_demo/src/photo-agent/`

---

## Phase 1: Setup

**Purpose**: Add environment variable scaffolding so all subsequent work has the right `.env` shape.

- [x] T001 Extend `.env.example` with FB_APP_ID, FB_APP_SECRET, FB_PAGE_ID, FB_PAGE_ACCESS_TOKEN, FB_REDIRECT_URI — with inline comments explaining each in `clients/_demo/src/photo-agent/.env.example`

**Checkpoint**: `.env.example` updated — developers can copy and fill in the new FB vars before running any new script.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The three new tool modules (`facebook_state.py`, `facebook_logger.py`, `facebook_api.py`) are shared by all three user stories. Nothing in Phase 3–5 can be implemented until these are in place.

**⚠️ CRITICAL**: Write tests first — they define the interface. Implement to make them pass.

### Tests (write first — must FAIL before implementation)

- [x] T002 [P] Write unit tests for `tools/facebook_state.py` covering: `set_pending_upload` (success, missing-key validation, duplicate idempotency key already in `published_idempotency_keys`), `get_pending_upload` (returns record, returns None when absent), `mark_uploading`, `mark_published` (adds key to `published_idempotency_keys`), `mark_failed`, `increment_attempt` (increments count and sets `last_attempt_at`), `is_published` (true/false), fcntl exclusive locking, FIELDKIT_DATA_DIR env override — in `tests/test_facebook_state.py`

- [x] T003 [P] Write unit tests for `tools/facebook_logger.py` covering: all six log functions (`log_upload_enqueued`, `log_upload_started`, `log_upload_published`, `log_upload_attempt_failed`, `log_upload_exhausted`, `log_token_expired`), pipe-delimited format integrity (no pipe chars in fields), FIELDKIT_LOG_DIR env override, and that no PII or token values appear in log output — in `tests/test_facebook_logger.py`

- [x] T004 [P] Write unit tests for `tools/facebook_api.py` covering: `build_auth_url` (includes required scopes, app_id, redirect_uri), `exchange_code_for_token` (mocked `requests.post` → returns token string, raises `FacebookUploadError` on non-OK), `exchange_for_long_lived_token` (mocked response), `get_page_access_token` (mocked `/me/accounts`, returns correct page token for given page_id, raises `FacebookUploadError` if page not found), `upload_video` (mocked multipart POST → returns post_id string, raises `FacebookTokenError` on FB error code 190, raises `FacebookUploadError` on HTTP 500 and network errors) — in `tests/test_facebook_api.py`

### Implementation (make tests pass)

- [x] T005 Implement `tools/facebook_state.py` — JSON state manager for `data/photo-agent/facebook_state.json` using same fcntl pattern as `state.py`; public API: `get_pending_upload() → dict | None`, `set_pending_upload(record: dict) → None` (validates required keys, checks idempotency), `mark_uploading(idempotency_key: str)`, `mark_published(idempotency_key: str, post_id: str)`, `mark_failed(idempotency_key: str)`, `increment_attempt(idempotency_key: str)`, `is_published(idempotency_key: str) → bool`; required record keys: `project_name`, `video_local_path`, `page_id`, `status`, `attempt_count`, `last_attempt_at`, `triggered_at`, `idempotency_key`, `fb_post_id`

- [x] T006 Implement `tools/facebook_logger.py` — activity log events appended to `logs/photo-agent.log` using same pipe-delimited format as `logger.py`; functions: `log_upload_enqueued(project_name)`, `log_upload_started(project_name, attempt)`, `log_upload_published(project_name, post_id)`, `log_upload_attempt_failed(project_name, attempt, error)`, `log_upload_exhausted(project_name)`, `log_token_expired(project_name)`; respects FIELDKIT_LOG_DIR env override

- [x] T007 Implement `tools/facebook_api.py` — Graph API v25.0 wrapper; exceptions: `FacebookTokenError(RuntimeError)` raised on FB OAuthException error_code 190 (token invalid/expired — skip retries), `FacebookUploadError(RuntimeError)` raised on all other failures; functions: `build_auth_url(app_id, redirect_uri, scopes, state_token) → str`, `exchange_code_for_token(code, app_id, app_secret, redirect_uri) → str`, `exchange_for_long_lived_token(short_token, app_id, app_secret) → str`, `get_page_access_token(long_user_token, page_id) → str` (calls `GET /me/accounts`), `upload_video(page_access_token, page_id, video_path) → str` (multipart POST to `https://graph.facebook.com/v25.0/{page_id}/videos`, returns post_id); all HTTP via `requests`, 60s timeout on upload

**Checkpoint**: All three tool modules implemented and their unit test suites green. Zero regressions on existing tests (`test_state.py`, `test_logger.py`, etc.).

---

## Phase 3: User Story 1 — Approved Video Auto-Posts to Facebook (Priority: P1) 🎯 MVP

**Goal**: When the owner taps Approve in Telegram, FieldKit enqueues a Facebook upload job. The upload cron picks it up, posts the video to the linked Facebook Page, and sends a Telegram confirmation with a direct link.

**Independent Test**: Approve a video in Telegram → verify `facebook_state.json` transitions `pending → uploading → published` → verify Telegram `sendMessage` is called with the Facebook post URL. (Mocked API; no real Facebook call needed.)

### Tests (write first)

- [x] T008 [P] [US1] Extend `tests/test_check_approval.py` with approve-path tests for FB enqueue: `facebook_state.set_pending_upload` is called with correct `project_name`, `video_local_path`, `page_id`, and `idempotency_key` (= str of `telegram_message_id`); idempotency skip: if `facebook_state.is_published(key)` returns True, `set_pending_upload` is NOT called; existing approve-path tests must still pass

- [x] T009 [P] [US1] Write `tests/test_upload_facebook.py` — happy-path unit tests: no pending job exits silently; pending job with `attempt_count=0` marks `uploading`, calls `facebook_api.upload_video`, marks `published`, logs `log_upload_published`, sends Telegram `sendMessage` with `https://www.facebook.com/{post_id}`; missing video file marks `failed` without calling upload API; env var validation (missing FB_PAGE_ACCESS_TOKEN or FB_PAGE_ID exits with code 1)

### Implementation

- [x] T010 [US1] Extend `scripts/check_approval.py` approve path — after `log_approved()`: import `facebook_state`, read `FB_PAGE_ID` from env, call `facebook_state.set_pending_upload({project_name, video_local_path, page_id, status: "pending", attempt_count: 0, last_attempt_at: null, triggered_at: now_iso8601, idempotency_key: str(telegram_message_id), fb_post_id: null})` guarded by `is_published(key)` check; log warning and skip if already published; failure to enqueue is logged as error but does NOT abort the existing approve flow

- [x] T011 [US1] Implement `scripts/upload_facebook.py` — cron script with `--source` arg; loads `.env`, validates FB_PAGE_ACCESS_TOKEN and FB_PAGE_ID; calls `facebook_state.get_pending_upload()` — exits silently if None; checks video file exists (marks failed + logs if missing); marks `uploading`; calls `facebook_api.upload_video(token, page_id, video_path)` → on success: calls `mark_published`, `log_upload_published`, sends Telegram `"✅ Video live on Facebook! {post_url}"` via `telegram_api.send_message` (add `send_message` to telegram_api.py if not present); on failure: see US3 phase for retry logic (stub as single-attempt for now — increment attempt, log, exit)

**Checkpoint**: End-to-end test: mock approve event → `check_approval._run()` → assert `facebook_state.json` has `status=pending` → call `upload_facebook.main()` → assert `status=published` and Telegram confirmation mock invoked.

---

## Phase 4: User Story 2 — Facebook Page Connection Setup (Priority: P2)

**Goal**: Admin runs one CLI command, which prints an auth URL to share with the owner. The owner authorizes in a browser, the local server catches the redirect, and the permanent Page access token is written to `.env` automatically.

**Independent Test**: Run `generate_auth_link.py` with a mocked local HTTP server that immediately returns `?code=test_code` → verify token exchange calls are made → verify `.env` is updated with `FB_PAGE_ID` and `FB_PAGE_ACCESS_TOKEN`.

### Tests (write first)

- [x] T012 [P] [US2] Write `tests/test_generate_auth_link.py` — unit tests (mocking `http.server`, `requests`, `.env` file writes): auth URL contains required scopes (`pages_show_list`, `pages_read_engagement`, `pages_manage_posts`), app_id, and redirect_uri; code exchange calls `facebook_api.exchange_code_for_token` then `exchange_for_long_lived_token` then `get_page_access_token`; `.env` write adds/updates `FB_PAGE_ID` and `FB_PAGE_ACCESS_TOKEN` without removing existing vars; missing `FB_APP_ID` exits with code 1; missing `FB_APP_SECRET` exits with code 1; no Page found exits with code 3; `--page-id` arg selects the correct Page when owner has multiple

### Implementation

- [x] T013 [US2] Implement `scripts/generate_auth_link.py` — CLI: `--port` (default 8080), `--page-id`; loads `.env`, validates `FB_APP_ID` and `FB_APP_SECRET` (exit 1 if missing); builds OAuth URL via `facebook_api.build_auth_url` with scopes `pages_show_list,pages_read_engagement,pages_manage_posts`; prints URL to stdout; starts `http.server.HTTPServer` on `localhost:PORT`, waits for GET to `/callback?code=...`; exchanges code for short token, then long token, then Page token via `facebook_api`; if `--page-id` given selects that Page, else picks first Page (prints name and ID); writes `FB_PAGE_ID` and `FB_PAGE_ACCESS_TOKEN` to `.env` (preserves all other vars); prints confirmation and exits 0; on OAuth failure exits 2; on Page selection failure exits 3

**Checkpoint**: Admin can run `python3 scripts/generate_auth_link.py` (with a test Facebook app), complete the flow in a browser, and see `FB_PAGE_ACCESS_TOKEN` written to `.env`.

---

## Phase 5: User Story 3 — Upload Failure Recovery (Priority: P3)

**Goal**: Transient upload failures are retried up to 3× with a 60-second cooldown. Token expiry skips retries. After all retries fail, the owner receives a Telegram alert.

**Independent Test**: Mock `facebook_api.upload_video` to raise `FacebookUploadError` three times → verify `attempt_count` increments to 3 → verify `status=failed` → verify Telegram alert sent. Mock `FacebookTokenError` → verify single attempt, `status=failed`, alert sent immediately.

### Tests (write first)

- [x] T014 [P] [US3] Extend `tests/test_upload_facebook.py` with failure-path tests: `FacebookUploadError` on first attempt increments `attempt_count` to 1, sets `last_attempt_at`, exits without alert; second call within 60s of `last_attempt_at` exits immediately (cooldown not elapsed); second call after 60s increments to 2; third failure sets `status=failed`, calls `log_upload_exhausted`, sends Telegram alert `"⚠️ Facebook upload failed for {project} after 3 attempts…"`; `FacebookTokenError` on any attempt sets `status=failed` immediately, calls `log_token_expired`, sends Telegram alert `"⚠️ Facebook token expired for {project} — reconnect your Page"`, does NOT increment attempt_count

### Implementation

- [x] T015 [US3] Extend `scripts/upload_facebook.py` with retry and failure logic — before calling upload API: read `attempt_count` and `last_attempt_at` from state; if `last_attempt_at` is set and `now - last_attempt_at < 60s` → exit silently (cooldown); on `FacebookUploadError`: call `facebook_state.increment_attempt()`; if `attempt_count` reaches 3: call `mark_failed`, `log_upload_exhausted`, send Telegram alert and exit; else exit (will retry next cron tick); on `FacebookTokenError`: call `mark_failed`, `log_token_expired`, send Telegram alert `"⚠️ Facebook token expired — reconnect {project_name}'s Page"` and exit

**Checkpoint**: All three failure scenarios (transient retry, retry exhaustion, token expiry) verified by test suite. All US1–US3 tests pass.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Operational docs, edge case hardening, and final test suite validation.

- [x] T016 [P] Create `SKILL_upload_facebook.md` — cron setup instructions (crontab entry matching cadence of `check_approval.py`), env var prerequisites checklist, manual invocation examples, and log file location — in `clients/_demo/src/photo-agent/SKILL_upload_facebook.md`

- [x] T017 [P] Create `SKILL_generate_auth_link.md` — step-by-step one-time setup: create Meta Developer App, set redirect URI, run script, verify `.env` — in `clients/_demo/src/photo-agent/SKILL_generate_auth_link.md`

- [x] T018 Run full test suite (`pytest clients/_demo/src/photo-agent/tests/ -v`) and confirm: all new tests pass, zero regressions in `test_check_approval.py`, `test_process_photos.py`, `test_state.py`, `test_logger.py`, `test_drive.py`, `test_telegram_api.py`, `test_video_generator.py`

**Checkpoint**: Feature 003 is complete. All 18 tasks done. Full test suite green.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1 — **BLOCKS all user stories**
- **Phase 3 (US1)**: Depends on Phase 2 complete
- **Phase 4 (US2)**: Depends on Phase 2 complete — independent of Phase 3
- **Phase 5 (US3)**: Depends on Phase 3 complete (extends `upload_facebook.py`)
- **Phase 6 (Polish)**: Depends on Phases 3–5 complete

### Within Phase 2

```
T002 ──→ T005    (test_facebook_state.py → facebook_state.py)
T003 ──→ T006    (test_facebook_logger.py → facebook_logger.py)
T004 ──→ T007    (test_facebook_api.py → facebook_api.py)

T002, T003, T004 can run in parallel (different files)
T005, T006, T007 can run in parallel (different files)
```

### Within Phase 3 (US1)

```
T008, T009 (tests) run in parallel
T010 depends on T005 (facebook_state.py) + T008 (test written)
T011 depends on T005, T006, T007 + T009 (test written)
```

### Parallel Opportunities

| Parallel group | Tasks |
|---|---|
| Phase 2 tests (write first) | T002, T003, T004 |
| Phase 2 implementations | T005, T006, T007 |
| Phase 3 tests | T008, T009 |
| Phase 4 tests | T012 (parallel with Phase 3 work) |
| Phase 6 docs | T016, T017 |

---

## Implementation Strategy

### MVP: User Story 1 Only

1. T001 — `.env.example`
2. T002 → T005 — `facebook_state.py`
3. T003 → T006 — `facebook_logger.py`
4. T004 → T007 — `facebook_api.py`
5. T008, T009 — US1 tests
6. T010 — extend `check_approval.py`
7. T011 — `upload_facebook.py` (single attempt, happy path)
8. **STOP and validate**: approve a video → confirm Facebook post + Telegram confirmation (mocked)

### Incremental Delivery

1. MVP (US1) → approved video posts to Facebook automatically
2. US2 → admin can authorize a new Facebook Page via CLI (one-time setup)
3. US3 → transient failures retry automatically; owner alerted on exhaustion

---

## Notes

- All new scripts follow the `load_dotenv` before FieldKit imports pattern from `check_approval.py`
- `send_message` (plain text, no buttons) may need adding to `telegram_api.py` — check first, add only if absent
- `FB_APP_SECRET` is used ONLY in `generate_auth_link.py`; it must NEVER appear in `upload_facebook.py` or be logged anywhere
- State file: `data/photo-agent/facebook_state.json` — created on first write, same DATA_DIR override as `state.json`
- Outside-code prereqs (non-blocking for all phases): Meta Developer App + test Facebook Page — admin creates these in parallel with implementation
