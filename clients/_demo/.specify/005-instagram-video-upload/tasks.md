# Tasks: Feature 005 — Instagram Video Upload

**Feature dir**: `clients/_demo/.specify/005-instagram-video-upload/`
**Implementation root**: `platform/photo-agent/` (shared, post-Platform-002-migration) for `tools/`, `scripts/`, `tests/`; `clients/_demo/src/photo-agent/.env` + `.env.example` for the one new per-client var
**Branch**: `002-instagram-video-upload`

**Prerequisites**: plan.md ✅ (revised), spec.md ✅, research.md ✅, data-model.md ✅ (revised), contracts/cli-contracts.md ✅ (revised), sequence-diagram.md ✅

**Revision note**: This task list was corrected after grounding against the current, post-Platform-002-migration codebase. Two things changed from an earlier draft: (1) all file paths now target `platform/photo-agent/` instead of the pre-migration `clients/_demo/src/photo-agent/{tools,scripts,tests}/`; (2) `instagram_state.py`'s design now mirrors `facebook_state.py`'s actual current atomic-claim API (`claim_pending_upload`/`release_claim`/`clear_pending_upload`/`mark_published`/`mark_failed`/`find_published`), not a simpler `mark_uploading`/`increment_attempt` shape. **T000 below is new** — it cherry-picks an orphaned, unclaimed prototype (branch `feature/instagram-api-client-sk`, commit `03d938f`) that already implements the container create→poll→publish flow with 12 passing tests; do this first so T004/T008 extend it instead of starting from zero.

**TDD required**: Constitution Gate 5 — tests written before or alongside implementation; a feature is NOT complete until all tests pass.

## Format: `[ID] [P?] [Story?] Description`

- **[P]**: Can run in parallel (different files, no shared state)
- **[Story]**: User story this task belongs to (US1/US2/US3)
- Paths are relative to `platform/photo-agent/` unless stated otherwise

---

## Phase 0: Reuse Existing Prototype

**Purpose**: Pull in an unclaimed, already-tested head start before doing any net-new work on `instagram_api.py`.

- [ ] T000 Cherry-pick commit `03d938f` ("Add Instagram Graph API client for Reels upload") from branch `feature/instagram-api-client-sk` onto this feature's branch (`002-instagram-video-upload`) — this adds `tools/instagram_api.py` (`upload_reel()` + private `_create_container`/`_poll_container_status`/`_publish_container` helpers, `InstagramUploadError`, `_GRAPH_BASE = "https://graph.facebook.com/v25.0"`, `_POLL_INTERVAL_SECONDS = 5`, `_MAX_POLL_ATTEMPTS = 60`) and `tests/test_instagram_api.py` (12 passing tests, all HTTP mocked). Resolve cleanly if it doesn't apply as-is (the branch was cut right after the watermark PR merged, so it should be close to current `main`). Confirm `pytest tests/test_instagram_api.py -v` passes before moving on. The `.worktrees/ig-api-sk` worktree can be removed once the cherry-pick is confirmed in this branch (`git worktree remove .worktrees/ig-api-sk`), or left in place — it does not need to stay checked out for this feature to proceed.

**Checkpoint**: `tools/instagram_api.py` and `tests/test_instagram_api.py` exist on this branch with the prototype's 12 tests green.

---

## Phase 1: Setup

**Purpose**: Add environment variable scaffolding so all subsequent work has the right `.env` shape.

- [ ] T001 Extend `.env.example` with `IG_BUSINESS_ACCOUNT_ID` — with an inline comment noting it is written by `check_instagram_connection.py` and that no new secret/token is introduced (Instagram publishing reuses `FB_PAGE_ACCESS_TOKEN` from Feature 003) — in `clients/_demo/src/photo-agent/.env.example`

**Checkpoint**: `.env.example` updated — developers can see the one new IG var before running any new script.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The four tool modules (`instagram_api.py` extension, `instagram_state.py`, `instagram_logger.py`, the `drive.py` share-link helper) are shared by all three user stories. Nothing in Phase 3–5 can be implemented until these are in place.

**⚠️ CRITICAL**: Write/extend tests first — they define the interface. Implement to make them pass.

### Tests (write first — must FAIL before implementation, except T004 which extends already-passing tests)

- [ ] T002 [P] Write unit tests for `tools/instagram_state.py` in `tests/test_instagram_state.py`, modeled directly on `tests/test_facebook_state.py`'s real coverage (read that file first): `set_pending_upload` (success, missing-key validation, duplicate idempotency key already in `published_idempotency_keys`), `get_pending_upload` (returns record, returns None when absent), `claim_pending_upload` (covers every documented return value: `"mismatch"`, `"in_flight"`, `"cooldown"`, `"stale_published"`, `"stale_failed"`, `"exhausted"`, `"claimed"` — including the cooldown-not-elapsed and max-attempts-exhausted paths), `release_claim`, `clear_pending_upload`, `mark_published` (adds key to `published_idempotency_keys`, clears `container_id`), `mark_failed` (clears `container_id`), `is_published`, `find_published`, `fcntl` exclusive locking, `FIELDKIT_DATA_DIR` env override (raises at import time if unset, matching `facebook_state.py`)

- [ ] T003 [P] Write unit tests for `tools/instagram_logger.py` covering: all eight log functions (`log_upload_enqueued`, `log_upload_started`, `log_container_created`, `log_container_ready`, `log_upload_published`, `log_upload_attempt_failed`, `log_upload_exhausted`, `log_token_expired`), pipe-delimited format integrity (no pipe chars in fields), `FIELDKIT_LOG_DIR` env override (raises at import time if unset, matching `facebook_logger.py`), and that no PII or token values appear in log output — in `tests/test_instagram_logger.py`

- [ ] T004 [P] Extend `tests/test_instagram_api.py` (already present after T000, 12 tests green) with tests for the gaps identified against `facebook_api.py`'s pattern: `InstagramTokenError` raised (not `InstagramUploadError`) when the Graph API returns error code 190, from each of the three call sites (container create, status poll, publish); `create_media_container`, `get_container_status`, `publish_container` are independently callable/importable public functions (not `_`-prefixed) with the same mocked-`requests` behavior the prototype's private helpers already have tests for; `discover_business_account(page_access_token, page_id)` — mocked `GET /{page_id}?fields=instagram_business_account` → returns `{"id", "username", "account_type"}` on success, raises `InstagramAccountNotFoundError` when no linked account or when linked account is not `BUSINESS`/`CREATOR`

- [ ] T005 [P] Write unit tests for the Drive share-link helper additions in `tools/drive.py` covering: `create_temporary_share_link(video_path)` (mocked Drive API → returns a publicly-reachable URL, sets `anyoneWithLink` reader permission), `revoke_share_link(file_id)` (mocked Drive API → removes the permission), and that both raise a clear error (not a silent no-op) if the underlying Drive call fails — in `tests/test_drive.py`

### Implementation (make tests pass)

- [ ] T006 Implement `tools/instagram_state.py` — JSON state manager for `$FIELDKIT_DATA_DIR/photo-agent/instagram_state.json`. **Read `tools/facebook_state.py`'s source first and mirror its structure/locking/claim-semantics as closely as possible** (adapt field names only — `ig_business_account_id` instead of `page_id`, `container_id` (new, Instagram-specific), `ig_post_id` instead of `fb_post_id`); public API: `get_pending_upload()`, `set_pending_upload(record)`, `claim_pending_upload(idempotency_key, *, cooldown_seconds, max_attempts, lease_seconds) -> str`, `release_claim(idempotency_key)`, `clear_pending_upload(expected_idempotency_key) -> bool`, `mark_published(idempotency_key, post_id)`, `mark_failed(idempotency_key)`, `is_published(idempotency_key) -> bool`, `find_published(project_name) -> dict | None`; raises plain `ValueError`/`RuntimeError` (no custom exception classes), matching `facebook_state.py`

- [ ] T007 Implement `tools/instagram_logger.py` — activity log events appended to `$FIELDKIT_LOG_DIR/photo-agent.log` using same pipe-delimited format as `facebook_logger.py`; functions: `log_upload_enqueued(project_name)`, `log_upload_started(project_name, attempt)`, `log_container_created(project_name, container_id)`, `log_container_ready(project_name, container_id)`, `log_upload_published(project_name, post_id)`, `log_upload_attempt_failed(project_name, attempt, error)`, `log_upload_exhausted(project_name)`, `log_token_expired(project_name)`; raises at import time if `FIELDKIT_LOG_DIR` unset, matching `facebook_logger.py`

- [ ] T008 Extend `tools/instagram_api.py` (present after T000) to close the confirmed gaps: add `InstagramTokenError(RuntimeError)`, raised on Graph API error code 190 from all three internal call sites (mirror `facebook_api.py`'s `if code == 190: raise FacebookTokenError(...)` check exactly); de-privatize `_create_container` → `create_media_container(page_access_token, ig_user_id, video_url) -> str`, `_poll_container_status` → `get_container_status(page_access_token, container_id) -> str`, `_publish_container` → `publish_container(page_access_token, ig_user_id, container_id) -> str` (keep `upload_reel()` as a thin wrapper over these if convenient, or remove it if `upload_instagram.py` calls the three steps directly — implementer's choice, but the three public functions are required by `contracts/cli-contracts.md`); add `discover_business_account(page_access_token, page_id) -> dict` and `InstagramAccountNotFoundError(RuntimeError)`; leave `_POLL_INTERVAL_SECONDS = 5` / `_MAX_POLL_ATTEMPTS = 60` (300s cap) as-is — `plan.md` has been updated to match this rather than the reverse

- [ ] T009 Extend `tools/drive.py` with `create_temporary_share_link(video_path) -> str` (uploads/locates the file in Drive, sets `anyoneWithLink` reader permission, returns the direct-download URL suitable for Instagram's `video_url`) and `revoke_share_link(file_id) -> None` (removes the `anyoneWithLink` permission) — reuses the existing Drive client/auth already configured for Feature 002's client-initiated uploads

**Checkpoint**: All four tool modules implemented/extended and their unit test suites green — including the prototype's original 12 tests in `test_instagram_api.py` still passing after T008's changes. Zero regressions on existing tests (`test_facebook_state.py`, `test_facebook_logger.py`, `test_facebook_api.py`, `test_drive.py`, etc.).

---

## Phase 3: User Story 1 — Approved Video Auto-Posts to Instagram (Priority: P1) 🎯 MVP

**Goal**: When the owner taps Approve in Telegram (via `check_approval.py`, synchronously shelled out to by Hermes's `photo-approve` skill), FieldKit enqueues an Instagram upload job alongside the existing Facebook job — no second approval. The `upload_instagram.py` cron script picks it up via an atomic claim, publishes the video as a Reel via the container flow, and sends a Telegram confirmation with a direct link.

**Independent Test**: Approve a video in Telegram → verify `instagram_state.json` transitions `pending → uploading → published` (via `claim_pending_upload` → `mark_published`) → verify Telegram `sendMessage` is called with the Instagram post URL. (Mocked API; no real Instagram call needed.)

### Tests (write first)

- [ ] T010 [P] [US1] Extend `tests/test_check_approval.py` with approve-path tests for IG enqueue: when `IG_BUSINESS_ACCOUNT_ID` is set, `instagram_state.set_pending_upload` is called with correct `project_name`, `video_local_path`, `ig_business_account_id`, and `idempotency_key` (= same value already used for the Facebook enqueue); idempotency skip: if `instagram_state.is_published(key)` returns True, `set_pending_upload` is NOT called; when `IG_BUSINESS_ACCOUNT_ID` is NOT set, `instagram_state.set_pending_upload` is never called (FR-016); existing Facebook enqueue and approve-path tests must still pass unchanged

- [ ] T011 [P] [US1] Write `tests/test_upload_instagram.py` — happy-path unit tests: `IG_BUSINESS_ACCOUNT_ID` not set exits `0` silently without touching state; `upload_instagram.lock` already held exits silently; no pending job exits silently; `claim_pending_upload` returning anything other than `"claimed"` (e.g. `"cooldown"`, `"in_flight"`) exits without any Drive/Instagram API call; a granted claim on a job with `attempt_count=0` calls `drive.create_temporary_share_link`, `instagram_api.create_media_container`, polls `get_container_status` until `FINISHED`, calls `publish_container`, calls `drive.revoke_share_link`, calls `mark_published`, logs `log_upload_published`, sends Telegram `sendMessage` with `https://www.instagram.com/p/{post_id}`; missing video file marks `failed` without calling the Drive/Instagram APIs; env var validation (missing `FB_PAGE_ACCESS_TOKEN` or `FIELDKIT_DATA_DIR`/`FIELDKIT_LOG_DIR` exits with code 1)

### Implementation

- [ ] T012 [US1] Extend `scripts/check_approval.py` approve path — after the existing `facebook_state.set_pending_upload(...)` call: read `IG_BUSINESS_ACCOUNT_ID` from env; if present and `not instagram_state.is_published(idempotency_key)`, call `instagram_state.set_pending_upload({project_name, video_local_path, ig_business_account_id, status: "pending", attempt_count: 0, last_attempt_at: null, triggered_at: now_iso8601, idempotency_key, container_id: null, ig_post_id: null})`; failure to enqueue is logged as error but does NOT abort the existing approve flow or the Facebook enqueue (FR-013)

- [ ] T013 [US1] Implement `scripts/upload_instagram.py` — cron script with `--source` arg, same `FIELDKIT_ROOT`/`CLIENT_NAME` boilerplate as `upload_facebook.py`; acquires `upload_instagram.lock` (re-entrancy guard, mirrors `upload_facebook.lock`); if `IG_BUSINESS_ACCOUNT_ID` not set, exits `0` silently; validates `FB_PAGE_ACCESS_TOKEN`; calls `instagram_state.get_pending_upload()` — exits silently if None; calls `instagram_state.claim_pending_upload(idempotency_key, cooldown_seconds=60, max_attempts=3, lease_seconds=...)` — acts only on `"claimed"`, exits silently on every other return value; checks video file exists (marks failed + logs if missing); calls `drive.create_temporary_share_link(video_path)` → `instagram_api.create_media_container(...)` → polls `get_container_status` → `instagram_api.publish_container(...)` → on success: `drive.revoke_share_link`, `mark_published`, `log_upload_published`, sends Telegram `"✅ Reel live on Instagram! {post_url}"` via `telegram_api.send_message`; on any failure: revoke the share link if one was created — full retry/exhaustion logic is Phase 5 (US3); for this phase, a single failed attempt may simply `release_claim` and exit (will retry next cron tick)

**Checkpoint**: End-to-end test: mock approve event → `check_approval._run()` → assert `instagram_state.json` has `status=pending` (and `facebook_state.json` is unaffected) → call `upload_instagram.main()` → assert `status=published` and Telegram confirmation mock invoked.

---

## Phase 4: User Story 2 — Instagram Account Connection Setup (Priority: P2)

**Goal**: Admin runs one CLI command against a client whose Facebook Page is already connected (Feature 003). It discovers the linked Instagram Business/Creator account and writes its ID to that client's `.env` — no OAuth flow.

**Independent Test**: Run `check_instagram_connection.py` with a mocked Graph API response → verify `IG_BUSINESS_ACCOUNT_ID` is written to `.env` on success, and that a clear, actionable message (not a stack trace) is printed when no eligible account is found.

### Tests (write first)

- [ ] T014 [P] [US2] Write `tests/test_check_instagram_connection.py` — unit tests (mocking `requests`, `.env` file writes): successful discovery writes `IG_BUSINESS_ACCOUNT_ID` to `.env` without removing existing vars, and prints the linked `@username`; `InstagramAccountNotFoundError` (no linked account) exits with code 3 and prints guidance to link one in Meta's Account Settings; linked account is `PERSONAL` (not Business/Creator) exits with code 3 and prints guidance to convert it; missing `FB_PAGE_ACCESS_TOKEN` or `FB_PAGE_ID` exits with code 1; `--page-id` arg overrides the `.env` value

### Implementation

- [ ] T015 [US2] Implement `scripts/check_instagram_connection.py` — CLI: `--page-id` (default from `FB_PAGE_ID` in `.env`), same `FIELDKIT_ROOT`/`CLIENT_NAME` boilerplate as other entrypoint scripts; loads `.env`, validates `FB_PAGE_ACCESS_TOKEN` and `FB_PAGE_ID`/`--page-id` (exit 1 if missing); calls `instagram_api.discover_business_account(token, page_id)`; on success (`account_type` in `{BUSINESS, CREATOR}`): writes `IG_BUSINESS_ACCOUNT_ID` to `.env` (preserves all other vars), prints confirmation with `@username` and account type, exits 0; on `InstagramAccountNotFoundError` or a `PERSONAL` account type: prints the appropriate actionable guidance message (per `contracts/cli-contracts.md`) and exits 3

**Checkpoint**: Admin can run `python3 scripts/check_instagram_connection.py` against a client whose Instagram account is linked and Business/Creator, and see `IG_BUSINESS_ACCOUNT_ID` written to that client's `.env`.

---

## Phase 5: User Story 3 — Upload Failure Recovery (Priority: P3)

**Goal**: Transient Instagram upload failures (including a stuck container) are retried up to 3× with a 60-second cooldown, via `claim_pending_upload`'s built-in cooldown/attempt-ceiling semantics — not hand-rolled timestamp math in the script. Token expiry skips retries. After all retries fail, the owner receives a Telegram alert — entirely independently of whatever happens to that same video's Facebook upload.

**Independent Test**: Mock `instagram_api.create_media_container` to raise `InstagramUploadError` three times (across three separate `claim_pending_upload` → attempt cycles) → verify the state ends at `status=failed` (i.e. `claim_pending_upload` eventually returns `"exhausted"` and the script calls `mark_failed`) → verify Telegram alert sent. Mock `InstagramTokenError` → verify single attempt, `status=failed`, alert sent immediately. Run the same scenario for a simulated Facebook success on the same video and confirm the Facebook job's state and confirmation message are unaffected.

### Tests (write first)

- [ ] T016 [P] [US3] Extend `tests/test_upload_instagram.py` with failure-path tests: an `InstagramUploadError` (from container create, poll timeout, or publish) results in the share link (if created) being revoked and the claim being released or exhausted appropriately, without a Telegram alert unless this was the final attempt; a `claim_pending_upload` call returning `"cooldown"` within 60s of the prior attempt results in no API calls and silent exit; three consecutive failed attempts (spaced ≥60s apart, i.e. `claim_pending_upload` eventually returns `"exhausted"` on what would be a 4th attempt, or the script itself detects `attempt_count == 3` after `release_claim`) results in `mark_failed`, `log_upload_exhausted`, and a Telegram alert containing `"Instagram upload failed"`; `InstagramTokenError` on any attempt calls `mark_failed` immediately (does not consume a retry), `log_token_expired`, sends a Telegram alert containing `"Instagram token expired"`; container status polling that never reaches `"FINISHED"` within 300s (60 attempts × 5s) raises `InstagramUploadError` and is handled exactly like any other transient failure

### Implementation

- [ ] T017 [US3] Extend `scripts/upload_instagram.py` with full retry and failure logic — wrap the container-create → poll → publish sequence so that any `InstagramUploadError` (including a poll-cap timeout) results in: revoke any share link created this attempt, then either `release_claim` (more attempts remain) or `mark_failed` + `log_upload_exhausted` + Telegram alert (this was the final attempt per `claim_pending_upload`'s own accounting — read `facebook_state.py`/`upload_facebook.py` to confirm exactly how the current implementation detects "this was the last attempt" and mirror it, rather than re-deriving the max-attempts check independently); on `InstagramTokenError`: revoke any share link, call `mark_failed`, `log_token_expired`, send Telegram alert `"⚠️ Instagram token expired — reconnect {project_name}'s account"` and exit

**Checkpoint**: All Instagram failure scenarios (transient retry, retry exhaustion, token expiry, container-poll timeout) verified by test suite, and confirmed independent of the Facebook job for the same video (FR-013). All US1–US3 tests pass.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Operational docs, the cross-platform-independence integration test, and final test suite validation.

- [ ] T018 [P] Create `docs/instagram/README.md` (mirroring the existing `docs/facebook/README.md` + `01-create-app.md` + `02-manual-test.md` pattern under `platform/photo-agent/docs/`) — covering: why no new Meta Developer App or OAuth setup is needed (reuses Feature 003's), how to link/convert an Instagram account to Business/Creator on a client's Facebook Page, how to run `check_instagram_connection.py`, cron setup for `upload_instagram.py` (crontab entry matching `upload_facebook.py`'s cadence), env var prerequisites (`IG_BUSINESS_ACCOUNT_ID`, reused `FB_PAGE_ACCESS_TOKEN`), the container-flow rationale (why a Drive share link is created and revoked), and log file location — in `platform/photo-agent/docs/instagram/README.md`

- [ ] T019 [P] Confirm whether a `SKILL.md` is warranted for either new script — per the Revision Note and `plan.md`, **`upload_instagram.py` should NOT get one** (it's cron-only, like `upload_facebook.py`, which also has none); `check_instagram_connection.py` is a one-time admin CLI like `generate_auth_link.py` (also no `SKILL.md`) — so this task is a documentation-placement decision, not new code: fold both scripts' usage instructions into T018's `docs/instagram/README.md` rather than creating `platform/photo-agent/skills/` entries, consistent with how Feature 003's equivalent scripts are documented

- [ ] T020 Write a dual-platform integration test in `tests/test_check_approval.py` (or a new `tests/test_dual_platform_integration.py` if that reads more clearly): approve a single video → verify both `facebook_state.json` and `instagram_state.json` receive pending records sharing the same `idempotency_key` → simulate an Instagram-only failure (all attempts exhausted) with a simulated Facebook success for the same video → assert the Facebook job reaches `published` with its normal confirmation sent, and the Instagram job reaches `failed` with its own alert sent, with neither job's mock calls referencing the other platform's state or lock file (FR-013, SC-007)

- [ ] T021 Run the full test suite (`pytest platform/photo-agent/tests/ -v`) and confirm: all new/extended tests pass, zero regressions in every existing test file (`test_check_approval.py` and its `_dispatch`/`_skill` siblings, `test_process_photos.py` and siblings, `test_state.py`, `test_logger.py`, `test_drive.py`, `test_telegram_api.py`, `test_video_generator.py`, `test_paths.py`, `test_env_loading.py`, `test_client_name_override.py`, `test_facebook_api.py`, `test_facebook_state.py`, `test_facebook_logger.py`, `test_generate_auth_link.py`, `test_upload_facebook.py`, `test_photo_approve_skill.py`/`_dispatch`, `test_photo_reject_skill.py`/`_dispatch`, and all others)

**Checkpoint**: Feature 005 is complete. All 22 tasks (T000–T021) done. Full test suite green.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 0 (Reuse prototype)**: No dependencies — start immediately, before Phase 1
- **Phase 1 (Setup)**: No dependencies — can run in parallel with Phase 0
- **Phase 2 (Foundational)**: Depends on Phase 0 (T004/T008 extend the cherry-picked prototype) and Phase 1 — **BLOCKS all user stories**
- **Phase 3 (US1)**: Depends on Phase 2 complete
- **Phase 4 (US2)**: Depends on Phase 2 complete — independent of Phase 3
- **Phase 5 (US3)**: Depends on Phase 3 complete (extends `upload_instagram.py`)
- **Phase 6 (Polish)**: Depends on Phases 3–5 complete

### Within Phase 2

```
T002 ──→ T006    (test_instagram_state.py → instagram_state.py)
T003 ──→ T007    (test_instagram_logger.py → instagram_logger.py)
T004 ──→ T008    (test_instagram_api.py extensions → instagram_api.py extensions)
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
| Phase 0 + Phase 1 | T000, T001 (independent files) |
| Phase 2 tests (write first) | T002, T003, T004, T005 |
| Phase 2 implementations | T006, T007, T008, T009 |
| Phase 3 tests | T010, T011 |
| Phase 4 tests | T014 (parallel with Phase 3 work — US2 is independent of US1) |
| Phase 6 docs | T018, T019 |

---

## Implementation Strategy

### MVP: User Story 1 Only

1. T000 — cherry-pick the orphaned `instagram_api.py` prototype
2. T001 — `.env.example`
3. T002 → T006 — `instagram_state.py`
4. T003 → T007 — `instagram_logger.py`
5. T004 → T008 — extend `instagram_api.py`
6. T005 → T009 — `drive.py` share-link helpers
7. T010, T011 — US1 tests
8. T012 — extend `check_approval.py`
9. T013 — `upload_instagram.py` (single attempt, happy path)
10. **STOP and validate**: approve a video → confirm Instagram Reel post + Telegram confirmation (mocked), alongside the existing Facebook post (Feature 003) from the same approval

### Incremental Delivery

1. MVP (US1) → approved video posts to Instagram automatically, from the same single approval that already posts to Facebook
2. US2 → admin can discover/link a client's Instagram account via one CLI run (no OAuth)
3. US3 → transient failures (including stuck containers) retry automatically via the claim-based cooldown/attempt-ceiling mechanism; owner alerted on exhaustion; verified independent of the Facebook job

---

## Notes

- All new/extended scripts follow the `load_dotenv` → `CLIENT_NAME` → per-client `.env` (`override=True`) boilerplate copy-pasted at the top of `process_photos.py`/`check_approval.py`/`upload_facebook.py` — single-client-at-a-time model (issue #61); do not attempt concurrent multi-client support
- `IG_BUSINESS_ACCOUNT_ID` absent = Instagram publishing silently disabled for that client (FR-016) — this is the mechanism that keeps `_construction_co` untouched by this feature without any client-name special-casing in the code
- `FB_PAGE_ACCESS_TOKEN` continues to be handled exactly as Feature 003 already handles it; this feature adds no new token-handling surface
- State file: `$FIELDKIT_DATA_DIR/photo-agent/instagram_state.json` — created on first write, same env-var resolution as `state.json` / `facebook_state.json` (no path-construction helper involved — `FIELDKIT_DATA_DIR`/`FIELDKIT_LOG_DIR` must be set in the client's `.env`, matching `facebook_state.py`/`facebook_logger.py`'s import-time validation)
- Outside-code prereqs (non-blocking for all phases): admin links/converts the Instagram account to Business/Creator on the client's existing Facebook Page in Meta's Account Settings — no new Meta Developer App work needed beyond Feature 003's; cron installation for `upload_instagram.py` is a deployment-time step, not a repo change
- Before starting, the implementer should independently confirm `tools/facebook_state.py`'s and `tools/facebook_api.py`'s exact current source (this task list's API descriptions were confirmed via a dedicated explore pass, but reading the source directly is authoritative over any secondhand description here)
