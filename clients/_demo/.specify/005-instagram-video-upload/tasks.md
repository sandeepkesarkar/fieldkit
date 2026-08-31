# Tasks: Feature 005 — Instagram Video Upload

**Feature dir**: `clients/_demo/.specify/005-instagram-video-upload/`
**Implementation root**: `clients/_demo/src/photo-agent/`
**Branch**: `002-instagram-video-upload`

**Prerequisites**: plan.md ✅, spec.md ✅, research.md ✅, data-model.md ✅, contracts/cli-contracts.md ✅, sequence-diagram.md ✅

**TDD required**: Constitution Gate 5 — tests written before or alongside implementation; a feature is NOT complete until all tests pass.

## Format: `[ID] [P?] [Story?] Description`

- **[P]**: Can run in parallel (different files, no shared state)
- **[Story]**: User story this task belongs to (US1/US2/US3)
- Paths are relative to `clients/_demo/src/photo-agent/`

---

## Phase 1: Setup

**Purpose**: Add environment variable scaffolding so all subsequent work has the right `.env` shape.

- [ ] T001 Extend `.env.example` with `IG_BUSINESS_ACCOUNT_ID` — with an inline comment noting it is written by `check_instagram_connection.py` and that no new secret/token is introduced (Instagram publishing reuses `FB_PAGE_ACCESS_TOKEN` from Feature 003) — in `clients/_demo/src/photo-agent/.env.example`

**Checkpoint**: `.env.example` updated — developers can see the one new IG var before running any new script.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The four new/extended tool modules (`instagram_state.py`, `instagram_logger.py`, `instagram_api.py`, and the Drive share-link helper) are shared by all three user stories. Nothing in Phase 3–5 can be implemented until these are in place.

**⚠️ CRITICAL**: Write tests first — they define the interface. Implement to make them pass.

### Tests (write first — must FAIL before implementation)

- [ ] T002 [P] Write unit tests for `tools/instagram_state.py` covering: `set_pending_upload` (success, missing-key validation, duplicate idempotency key already in `published_idempotency_keys`), `get_pending_upload` (returns record, returns None when absent), `mark_uploading`, `mark_published` (adds key to `published_idempotency_keys`, clears `container_id`), `mark_failed` (clears `container_id`), `increment_attempt` (increments count and sets `last_attempt_at`), `is_published` (true/false), fcntl exclusive locking, FIELDKIT_DATA_DIR env override — in `tests/test_instagram_state.py`

- [ ] T003 [P] Write unit tests for `tools/instagram_logger.py` covering: all eight log functions (`log_upload_enqueued`, `log_upload_started`, `log_container_created`, `log_container_ready`, `log_upload_published`, `log_upload_attempt_failed`, `log_upload_exhausted`, `log_token_expired`), pipe-delimited format integrity (no pipe chars in fields), FIELDKIT_LOG_DIR env override, and that no PII or token values appear in log output — in `tests/test_instagram_logger.py`

- [ ] T004 [P] Write unit tests for `tools/instagram_api.py` covering: `discover_business_account` (mocked `GET /{page_id}?fields=instagram_business_account` → returns account ID + account type; raises `InstagramAccountNotFoundError` when no linked account or when linked account is not BUSINESS/CREATOR), `create_media_container` (mocked `POST /{ig_user_id}/media` → returns container_id string, raises `InstagramTokenError` on error code 190, raises `InstagramUploadError` on HTTP 500/network errors), `get_container_status` (mocked `GET /{container_id}?fields=status_code` → returns status string), `publish_container` (mocked `POST /{ig_user_id}/media_publish` → returns post_id string, raises `InstagramTokenError`/`InstagramUploadError` analogous to container creation) — in `tests/test_instagram_api.py`

- [ ] T005 [P] Write unit tests for the Drive share-link helper additions in `tools/drive.py` covering: `create_temporary_share_link(video_path)` (mocked Drive API → returns a publicly-reachable URL, sets `anyoneWithLink` reader permission), `revoke_share_link(url_or_file_id)` (mocked Drive API → removes the permission), and that both raise a clear error (not a silent no-op) if the underlying Drive call fails — in `tests/test_drive.py`

### Implementation (make tests pass)

- [ ] T006 Implement `tools/instagram_state.py` — JSON state manager for `data/photo-agent/instagram_state.json` using same fcntl pattern as `facebook_state.py`; public API: `get_pending_upload() → dict | None`, `set_pending_upload(record: dict) → None` (validates required keys, checks idempotency), `mark_uploading(idempotency_key: str)`, `mark_published(idempotency_key: str, post_id: str)`, `mark_failed(idempotency_key: str)`, `increment_attempt(idempotency_key: str)`, `is_published(idempotency_key: str) → bool`; required record keys: `project_name`, `video_local_path`, `ig_business_account_id`, `status`, `attempt_count`, `last_attempt_at`, `triggered_at`, `idempotency_key`, `container_id`, `ig_post_id`

- [ ] T007 Implement `tools/instagram_logger.py` — activity log events appended to `logs/photo-agent.log` using same pipe-delimited format as `facebook_logger.py`; functions: `log_upload_enqueued(project_name)`, `log_upload_started(project_name, attempt)`, `log_container_created(project_name, container_id)`, `log_container_ready(project_name, container_id)`, `log_upload_published(project_name, post_id)`, `log_upload_attempt_failed(project_name, attempt, error)`, `log_upload_exhausted(project_name)`, `log_token_expired(project_name)`; respects FIELDKIT_LOG_DIR env override

- [ ] T008 Implement `tools/instagram_api.py` — Graph API v25.0 wrapper; exceptions: `InstagramTokenError(RuntimeError)` raised on OAuthException error_code 190 (token invalid/expired — skip retries), `InstagramUploadError(RuntimeError)` raised on all other retryable failures, `InstagramAccountNotFoundError(RuntimeError)` raised only by `discover_business_account`; functions: `discover_business_account(page_access_token, page_id) → dict` (returns `{"id": ..., "username": ..., "account_type": ...}`, calling `GET /{page_id}?fields=instagram_business_account{...}`), `create_media_container(page_access_token, ig_user_id, video_url) → str` (`POST /{ig_user_id}/media` with `media_type=REELS`), `get_container_status(page_access_token, container_id) → str` (`GET /{container_id}?fields=status_code`), `publish_container(page_access_token, ig_user_id, container_id) → str` (`POST /{ig_user_id}/media_publish`); all HTTP via `requests`, 30s timeout per call

- [ ] T009 Extend `tools/drive.py` with `create_temporary_share_link(video_path) → str` (uploads/locates the file in Drive, sets `anyoneWithLink` reader permission, returns the direct-download URL suitable for Instagram's `video_url`) and `revoke_share_link(file_id) → None` (removes the `anyoneWithLink` permission) — reuses the existing Drive client/auth already configured for Feature 002's client-initiated uploads

**Checkpoint**: All four tool modules implemented and their unit test suites green. Zero regressions on existing tests (`test_facebook_state.py`, `test_facebook_logger.py`, `test_facebook_api.py`, `test_drive.py`, etc.).

---

## Phase 3: User Story 1 — Approved Video Auto-Posts to Instagram (Priority: P1) 🎯 MVP

**Goal**: When the owner taps Approve in Telegram, FieldKit enqueues an Instagram upload job alongside the existing Facebook job — no second approval. The upload cron picks it up, publishes the video as a Reel via the container flow, and sends a Telegram confirmation with a direct link.

**Independent Test**: Approve a video in Telegram → verify `instagram_state.json` transitions `pending → uploading → published` → verify Telegram `sendMessage` is called with the Instagram post URL. (Mocked API; no real Instagram call needed.)

### Tests (write first)

- [ ] T010 [P] [US1] Extend `tests/test_check_approval.py` with approve-path tests for IG enqueue: when `IG_BUSINESS_ACCOUNT_ID` is set, `instagram_state.set_pending_upload` is called with correct `project_name`, `video_local_path`, `ig_business_account_id`, and `idempotency_key` (= same value already used for the Facebook enqueue — same Telegram `message_id`); idempotency skip: if `instagram_state.is_published(key)` returns True, `set_pending_upload` is NOT called; when `IG_BUSINESS_ACCOUNT_ID` is NOT set, `instagram_state.set_pending_upload` is never called (FR-016); existing Facebook enqueue and approve-path tests must still pass unchanged

- [ ] T011 [P] [US1] Write `tests/test_upload_instagram.py` — happy-path unit tests: `IG_BUSINESS_ACCOUNT_ID` not set exits `0` silently without touching state; no pending job exits silently; pending job with `attempt_count=0` marks `uploading`, calls `drive.create_temporary_share_link`, calls `instagram_api.create_media_container`, polls `get_container_status` until `FINISHED`, calls `publish_container`, calls `drive.revoke_share_link`, marks `published`, logs `log_upload_published`, sends Telegram `sendMessage` with `https://www.instagram.com/p/{post_id}`; missing video file marks `failed` without calling the Drive/Instagram APIs; env var validation (missing `FB_PAGE_ACCESS_TOKEN` exits with code 1)

### Implementation

- [ ] T012 [US1] Extend `scripts/check_approval.py` approve path — after the existing `facebook_state.set_pending_upload(...)` call: read `IG_BUSINESS_ACCOUNT_ID` from env; if present and `not instagram_state.is_published(idempotency_key)`, call `instagram_state.set_pending_upload({project_name, video_local_path, ig_business_account_id, status: "pending", attempt_count: 0, last_attempt_at: null, triggered_at: now_iso8601, idempotency_key, container_id: null, ig_post_id: null})`; failure to enqueue is logged as error but does NOT abort the existing approve flow or the Facebook enqueue (FR-013)

- [ ] T013 [US1] Implement `scripts/upload_instagram.py` — cron script with `--source` arg; loads `.env`; if `IG_BUSINESS_ACCOUNT_ID` not set, exits `0` silently; validates `FB_PAGE_ACCESS_TOKEN`; calls `instagram_state.get_pending_upload()` — exits silently if None; checks video file exists (marks failed + logs if missing); marks `uploading`; calls `drive.create_temporary_share_link(video_path)` → `instagram_api.create_media_container(token, ig_business_account_id, video_url)` → polls `get_container_status` (5s interval, 3-minute cap; treats timeout as `InstagramUploadError`) → `instagram_api.publish_container(...)` → on success: calls `drive.revoke_share_link`, `mark_published`, `log_upload_published`, sends Telegram `"✅ Reel live on Instagram! {post_url}"` via `telegram_api.send_message`; on any failure: revokes the share link if one was created, see US3 phase for full retry logic (stub as single-attempt for now — increment attempt, log, exit)

**Checkpoint**: End-to-end test: mock approve event → `check_approval._run()` → assert `instagram_state.json` has `status=pending` (and `facebook_state.json` is unaffected) → call `upload_instagram.main()` → assert `status=published` and Telegram confirmation mock invoked.

---

## Phase 4: User Story 2 — Instagram Account Connection Setup (Priority: P2)

**Goal**: Admin runs one CLI command against a client whose Facebook Page is already connected (Feature 003). It discovers the linked Instagram Business/Creator account and writes its ID to `.env` — no OAuth flow.

**Independent Test**: Run `check_instagram_connection.py` with a mocked Graph API response → verify `IG_BUSINESS_ACCOUNT_ID` is written to `.env` on success, and that a clear, actionable message (not a stack trace) is printed when no eligible account is found.

### Tests (write first)

- [ ] T014 [P] [US2] Write `tests/test_check_instagram_connection.py` — unit tests (mocking `requests`, `.env` file writes): successful discovery writes `IG_BUSINESS_ACCOUNT_ID` to `.env` without removing existing vars, and prints the linked `@username`; no linked Instagram account exits with code 3 and prints guidance to link one in Meta's Account Settings; linked account is PERSONAL (not Business/Creator) exits with code 3 and prints guidance to convert it; missing `FB_PAGE_ACCESS_TOKEN` or `FB_PAGE_ID` exits with code 1; `--page-id` arg overrides the `.env` value

### Implementation

- [ ] T015 [US2] Implement `scripts/check_instagram_connection.py` — CLI: `--page-id` (default from `FB_PAGE_ID` in `.env`); loads `.env`, validates `FB_PAGE_ACCESS_TOKEN` and `FB_PAGE_ID`/`--page-id` (exit 1 if missing); calls `instagram_api.discover_business_account(token, page_id)`; on success (account_type in `{BUSINESS, CREATOR}`): writes `IG_BUSINESS_ACCOUNT_ID` to `.env` (preserves all other vars), prints confirmation with `@username` and account type, exits 0; on `InstagramAccountNotFoundError` or a PERSONAL account type: prints the appropriate actionable guidance message (per `contracts/cli-contracts.md`) and exits 3

**Checkpoint**: Admin can run `python3 scripts/check_instagram_connection.py` against a client whose Instagram account is linked and Business/Creator, and see `IG_BUSINESS_ACCOUNT_ID` written to `.env`.

---

## Phase 5: User Story 3 — Upload Failure Recovery (Priority: P3)

**Goal**: Transient Instagram upload failures (including a stuck container) are retried up to 3× with a 60-second cooldown. Token expiry skips retries. After all retries fail, the owner receives a Telegram alert — entirely independently of whatever happens to that same video's Facebook upload.

**Independent Test**: Mock `instagram_api.create_media_container` to raise `InstagramUploadError` three times → verify `attempt_count` increments to 3 → verify `status=failed` → verify Telegram alert sent. Mock `InstagramTokenError` → verify single attempt, `status=failed`, alert sent immediately. Run the same scenario for a simulated Facebook success on the same video and confirm the Facebook job's state and confirmation message are unaffected.

### Tests (write first)

- [ ] T016 [P] [US3] Extend `tests/test_upload_instagram.py` with failure-path tests: `InstagramUploadError` on first attempt (container create, poll timeout, or publish) increments `attempt_count` to 1, sets `last_attempt_at`, revokes any share link created during that attempt, exits without alert; second call within 60s of `last_attempt_at` exits immediately (cooldown not elapsed); second call after 60s increments to 2; third failure sets `status=failed`, calls `log_upload_exhausted`, sends Telegram alert `"⚠️ Instagram upload failed for {project} after 3 attempts…"`; `InstagramTokenError` on any attempt sets `status=failed` immediately, calls `log_token_expired`, sends Telegram alert `"⚠️ Instagram token expired for {project} — reconnect your account"`, does NOT increment attempt_count; container status polling that never reaches `FINISHED` within 3 minutes raises `InstagramUploadError` and is retried like any other transient failure

### Implementation

- [ ] T017 [US3] Extend `scripts/upload_instagram.py` with retry and failure logic — before calling the Instagram APIs: read `attempt_count` and `last_attempt_at` from state; if `last_attempt_at` is set and `now - last_attempt_at < 60s` → exit silently (cooldown); wrap the container-create → poll → publish sequence so any `InstagramUploadError` (including a poll timeout, implemented as a bounded loop that raises after 3 minutes) results in: revoke any share link created this attempt, call `instagram_state.increment_attempt()`; if `attempt_count` reaches 3: call `mark_failed`, `log_upload_exhausted`, send Telegram alert and exit; else exit (will retry next cron tick); on `InstagramTokenError`: revoke any share link, call `mark_failed`, `log_token_expired`, send Telegram alert `"⚠️ Instagram token expired — reconnect {project_name}'s account"` and exit

**Checkpoint**: All Instagram failure scenarios (transient retry, retry exhaustion, token expiry, container-poll timeout) verified by test suite, and confirmed independent of the Facebook job for the same video (FR-013). All US1–US3 tests pass.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Operational docs, the cross-platform-independence integration test, and final test suite validation.

- [ ] T018 [P] Create `SKILL_upload_instagram.md` — cron setup instructions (crontab entry matching cadence of `upload_facebook.py`), env var prerequisites checklist (`IG_BUSINESS_ACCOUNT_ID`, reused `FB_PAGE_ACCESS_TOKEN`), manual invocation examples, container-flow notes (why a Drive share link is created and revoked), and log file location — in `clients/_demo/src/photo-agent/SKILL_upload_instagram.md`

- [ ] T019 [P] Create `SKILL_check_instagram_connection.md` — step-by-step one-time setup: convert/link the Instagram account to the client's Facebook Page in Meta's Account Settings, run the script, verify `.env` — in `clients/_demo/src/photo-agent/SKILL_check_instagram_connection.md`

- [ ] T020 Write a dual-platform integration test in `tests/test_check_approval.py` (or a new `tests/test_dual_platform_integration.py` if that reads more clearly): approve a single video → verify both `facebook_state.json` and `instagram_state.json` receive pending records sharing the same `idempotency_key` → simulate an Instagram-only failure (all 3 attempts) with a simulated Facebook success for the same video → assert the Facebook job reaches `published` with its normal confirmation sent, and the Instagram job reaches `failed` with its own alert sent, with neither job's mock calls referencing the other platform's state (FR-013, SC-007)

- [ ] T021 Run full test suite (`pytest clients/_demo/src/photo-agent/tests/ -v`) and confirm: all new tests pass, zero regressions in every existing test file (`test_check_approval.py`, `test_process_photos.py`, `test_state.py`, `test_logger.py`, `test_drive.py`, `test_telegram_api.py`, `test_video_generator.py`, `test_facebook_api.py`, `test_facebook_state.py`, `test_facebook_logger.py`, `test_generate_auth_link.py`, `test_upload_facebook.py`)

**Checkpoint**: Feature 005 is complete. All 21 tasks done. Full test suite green.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1 — **BLOCKS all user stories**
- **Phase 3 (US1)**: Depends on Phase 2 complete
- **Phase 4 (US2)**: Depends on Phase 2 complete — independent of Phase 3
- **Phase 5 (US3)**: Depends on Phase 3 complete (extends `upload_instagram.py`)
- **Phase 6 (Polish)**: Depends on Phases 3–5 complete

### Within Phase 2

```
T002 ──→ T006    (test_instagram_state.py → instagram_state.py)
T003 ──→ T007    (test_instagram_logger.py → instagram_logger.py)
T004 ──→ T008    (test_instagram_api.py → instagram_api.py)
T005 ──→ T009    (test_drive.py additions → drive.py additions)

T002, T003, T004, T005 can run in parallel (different files)
T006, T007, T008, T009 can run in parallel (different files)
```

### Within Phase 3 (US1)

```
T010, T011 (tests) run in parallel
T012 depends on T006 (instagram_state.py) + T010 (test written)
T013 depends on T006, T007, T008, T009 + T011 (test written)
```

### Parallel Opportunities

| Parallel group | Tasks |
|---|---|
| Phase 2 tests (write first) | T002, T003, T004, T005 |
| Phase 2 implementations | T006, T007, T008, T009 |
| Phase 3 tests | T010, T011 |
| Phase 4 tests | T014 (parallel with Phase 3 work — US2 is independent of US1) |
| Phase 6 docs | T018, T019 |

---

## Implementation Strategy

### MVP: User Story 1 Only

1. T001 — `.env.example`
2. T002 → T006 — `instagram_state.py`
3. T003 → T007 — `instagram_logger.py`
4. T004 → T008 — `instagram_api.py`
5. T005 → T009 — `drive.py` share-link helpers
6. T010, T011 — US1 tests
7. T012 — extend `check_approval.py`
8. T013 — `upload_instagram.py` (single attempt, happy path)
9. **STOP and validate**: approve a video → confirm Instagram Reel post + Telegram confirmation (mocked), alongside the existing Facebook post (Feature 003) from the same approval

### Incremental Delivery

1. MVP (US1) → approved video posts to Instagram automatically, from the same single approval that already posts to Facebook
2. US2 → admin can discover/link a client's Instagram account via one CLI run (no OAuth)
3. US3 → transient failures (including stuck containers) retry automatically; owner alerted on exhaustion; verified independent of the Facebook job

---

## Notes

- All new scripts follow the `load_dotenv` before FieldKit imports pattern from `check_approval.py`
- `IG_BUSINESS_ACCOUNT_ID` absent = Instagram publishing silently disabled for that client (FR-016) — this is the mechanism that keeps `_construction_co` untouched by this feature without any client-name special-casing in the code
- `FB_PAGE_ACCESS_TOKEN` continues to be handled exactly as Feature 003 already handles it; this feature adds no new token-handling surface
- State file: `data/photo-agent/instagram_state.json` — created on first write, same DATA_DIR override as `state.json` / `facebook_state.json`
- Outside-code prereqs (non-blocking for all phases): admin links/converts the Instagram account to Business/Creator on the client's existing Facebook Page in Meta's Account Settings — no new Meta Developer App work needed beyond Feature 003's
