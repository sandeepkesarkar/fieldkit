# 005 — Instagram Video Upload: Technical Plan

**Status:** Technical Planning
**Spec:** [`spec.md`](spec.md)
**Sequence diagram:** [`sequence-diagram.md`](sequence-diagram.md)
**Last Updated:** 2026-08-31 (revised after grounding against current `platform/photo-agent/` code — see Revision Note)

---

## Revision Note (important — read before implementing)

This plan was originally drafted by analogy to `clients/_demo/.specify/003-facebook-upload`, which predates **Platform Feature 002** (the photo-agent migration, already merged to `main`). Post-migration, all photo-agent *code* (tools/scripts/tests) lives in `platform/photo-agent/`, shared across clients — only per-client `.env` files and `data/`/`logs/` remain under `clients/{name}/`. This revision corrects every path and API-shape assumption below against the current code, confirmed by a dedicated explore pass. Two load-bearing corrections:

1. **Implementation root is `platform/photo-agent/`, not `clients/_demo/src/photo-agent/`.** Only `.env` / `.env.example` stay under `clients/_demo/src/photo-agent/` (per-client credentials); everything else — `tools/`, `scripts/`, `tests/` — is shared platform code.
2. **`facebook_state.py`'s real API is atomic-claim-based, not the simple `mark_uploading`/`increment_attempt` shape Feature 003's original docs described.** Its confirmed public surface is: `get_pending_upload()`, `set_pending_upload(record)`, `claim_pending_upload(idempotency_key, *, cooldown_seconds, max_attempts, lease_seconds) -> str` (returns one of `"mismatch" | "in_flight" | "cooldown" | "stale_published" | "stale_failed" | "exhausted" | "claimed"`), `release_claim(idempotency_key)`, `clear_pending_upload(expected_idempotency_key) -> bool`, `mark_published(idempotency_key, post_id)`, `mark_failed(idempotency_key)`, `is_published(idempotency_key) -> bool`, `find_published(project_name) -> dict | None`. `claim_pending_upload` is the single atomic compare-and-transition entrypoint — `instagram_state.py` MUST mirror this exact function family (same names, same semantics, adapted field names), not reinvent a simpler one. Read `platform/photo-agent/tools/facebook_state.py`'s source directly before implementing — it is the authoritative interface to mirror, more so than this document's prose.

**Also discovered: an orphaned, unclaimed head start already exists.** Branch `feature/instagram-api-client-sk` (worktree `.worktrees/ig-api-sk`, commit `03d938f`, message "Add Instagram Graph API client for Reels upload") already implements `platform/photo-agent/tools/instagram_api.py` + `platform/photo-agent/tests/test_instagram_api.py` (193 + 294 lines, 12 passing tests, all HTTP mocked). Confirmed via `gh pr list` / `gh issue list` / repo-wide grep: **nothing references this branch anywhere** — no PR, no issue, no doc. It is not claimed or in progress by anyone else. **The implementer should cherry-pick commit `03d938f` onto this feature's branch and extend it**, rather than write `instagram_api.py` from scratch — see Key Design Decisions and `tasks.md` T004/T008 for the specific gaps to close (it currently covers only the upload/publish half of Phase 1's tool work, with three concrete deviations from this plan that need reconciling — see below).

---

## Summary

Extend the `_demo` pipeline so that the single Telegram approval which already triggers a Facebook post (Feature 003) also triggers an Instagram Reel publish, via the Instagram Graph API. Instagram publishing is authenticated through the Facebook Page access token already stored for Feature 003 — no new OAuth flow — and the two platform uploads are independent queues, jobs, and failure domains, mirroring Feature 003's architecture (and its current, post-migration implementation) as closely as possible.

---

## Stack

| Concern | Solution | Rationale |
|---------|----------|-----------|
| Instagram API client | `requests` — direct Graph API v25.0 (`graph.facebook.com`), extending the existing orphaned prototype (see Revision Note) | Already a dependency; consistent with `facebook_api.py`; no SDK needed; reuses 12 already-passing tests instead of starting at zero |
| Account discovery | `GET /{page_id}?fields=instagram_business_account` using the existing FB Page token | Reuses Feature 003 credential; zero new OAuth surface; **not present in the prototype — net-new work** |
| Video source for publish | Temporary Google Drive shareable link to the already-approved, already-metadata-stripped video file | Instagram Graph API's media-container creation requires a publicly reachable `video_url`, unlike Facebook's direct multipart upload; Drive is the framework's existing sanctioned exception for hosting client-approved media (root constitution, Architecture Constraints). **Not present in the prototype** — `upload_reel(access_token, ig_user_id, video_url)` takes `video_url` as a plain caller-supplied argument with zero Drive knowledge; the share-link helper in `tools/drive.py` must be built from scratch |
| Publish flow | Two-step async: `POST /{ig_user_id}/media` (create container) → poll `GET /{container_id}?fields=status_code` → `POST /{ig_user_id}/media_publish` | Required by Instagram Graph API for video/Reels; already implemented in the prototype as private helpers `_create_container` / `_poll_container_status` / `_publish_container` behind one public `upload_reel()` — must be de-privatized into independently callable/testable functions (`create_media_container`, `get_container_status`, `publish_container`) per `contracts/cli-contracts.md` |
| State persistence | `instagram_state.json` + `fcntl` locking, atomic-claim API mirroring the CURRENT `facebook_state.py` (see Revision Note) | Kept as a separate file from `facebook_state.json` so the two platforms' jobs never block each other (FR-013); same claim/cooldown/lease semantics, not the older simpler shape |
| Retry logic | `claim_pending_upload(..., cooldown_seconds=60, max_attempts=3, lease_seconds=...)` inside the cron script | Matches `upload_facebook.py`'s current pattern exactly; no threads, no hand-rolled timestamp math in the script itself |
| Cron re-entrancy | `upload_instagram.lock` file, same pattern as `upload_facebook.lock` | `upload_facebook.py`'s own docstring documents this as guarding against overlapping cron invocations (issue #34 follow-up) — Instagram's cron script needs the identical guard |
| Token storage | `.env` (under `clients/_demo/src/photo-agent/.env`, per-client) — reuses `FB_PAGE_ACCESS_TOKEN`; adds `IG_BUSINESS_ACCOUNT_ID` only | Consistent with existing credential pattern; no new secret class; confirmed absent from the current `.env.example` (T001 is still pending, unaffected by the above corrections) |
| Logging | `instagram_logger.py` → `photo-agent.log`, resolved via `$FIELDKIT_LOG_DIR` (per-client, e.g. `clients/_demo/logs/photo-agent.log`) exactly like `facebook_logger.py` | Single per-client log per spec (FR-012); confirmed both `FIELDKIT_DATA_DIR` and `FIELDKIT_LOG_DIR` are the live env-var mechanism, not a path-construction helper |
| Container polling timeout | Poll every 5s, cap at **5 minutes (300s) / 60 attempts** — matches the prototype's already-implemented, already-tested `_POLL_INTERVAL_SECONDS = 5` / `_MAX_POLL_ATTEMPTS = 60`, adopted here rather than reworked | Original draft said 3 minutes; changed to match the existing tested implementation rather than force a rewrite for no functional benefit — still comfortably bounds the "stuck container" edge case for 20–60s source videos |
| Invocation model | `upload_instagram.py` is **cron-invoked**, like `upload_facebook.py` — NOT wired into any Hermes skill | Confirmed: `check_approval.py` moved to synchronous skill invocation (issue #49), but `upload_facebook.py` explicitly did not — its docstring, `facebook_api.py`'s docstring, and `clients/venus/README.md` all still describe/require cron invocation. No `SKILL.md` exists for it under `platform/photo-agent/skills/`, and none should be added for `upload_instagram.py` either |
| Per-client resolution | Same `FIELDKIT_ROOT` / `CLIENT_NAME` / `load_dotenv` boilerplate already copy-pasted at the top of `process_photos.py`, `check_approval.py`, `upload_facebook.py` | No new resolution mechanism — `upload_instagram.py` and `check_instagram_connection.py` copy the identical block (single-client-at-a-time model per issue #61; do not attempt concurrent multi-client support) |

---

## Architecture

Feature 005 follows the same cron-script pattern as `upload_facebook.py`, kept as a parallel, independent track rather than woven into the Facebook code path. `check_approval.py` (synchronously invoked by Hermes's `photo-approve`/`photo-reject` skills, not cron) gains a second enqueue call on the approve path, alongside the existing `facebook_state.set_pending_upload()` call: `instagram_state.set_pending_upload()`. A new cron script, `upload_instagram.py`, runs on the same cadence as `upload_facebook.py`, guarded by its own `upload_instagram.lock`, and drains the Instagram queue independently via `instagram_state.claim_pending_upload(...)` — creating a temporary Drive share link for the approved video, creating an Instagram media container, polling until Instagram finishes processing it (5s interval, 300s cap), publishing it, and sending a Telegram confirmation. Failures, retries, and token-expiry handling are scoped to the Instagram job only (FR-013): a Facebook failure never blocks or retries the Instagram job and vice versa, because the two use entirely separate state files, lock files, and claim namespaces. A one-time admin CLI script, `check_instagram_connection.py`, discovers the Instagram professional account linked to the client's already-connected Facebook Page and writes its ID to `.env` — no new OAuth dance, since Instagram Graph API access rides on the Page token FieldKit already holds from Feature 003.

All new/extended code lives in `platform/photo-agent/{tools,scripts,tests}/` (shared across clients); only the new `IG_BUSINESS_ACCOUNT_ID` var lives in `clients/_demo/src/photo-agent/.env`.

---

## Constitution Check

*All gates must pass before implementation begins.*

- [x] **Privacy**: The video published to Instagram is the same already-approved, already-metadata-stripped asset used for the Facebook post (FR-014) — no re-processing, no new metadata exposure. The temporary Drive share link used only to satisfy Instagram's `video_url` requirement is scoped to the single approved video, created immediately before the container call and revoked immediately after publish (or after final failure) so it is not left reachable.
- [x] **HITL**: No second approval gate is introduced — Instagram publishing rides the same Telegram approval tap that already gates Facebook publishing (FR-002). FieldKit never publishes to Instagram without that prior human approval.
- [x] **Budget**: No AI/LLM API calls in this feature — same as Feature 003. The only external calls are to the Instagram Graph API (free for publishing) and Google Drive API (already used, free tier) and Telegram Bot API (free). FR-015's pause-on-exhaustion requirement is satisfied by the pipeline's existing shared budget-guard check (already invoked before any per-client cron work runs); this feature adds no new AI spend to guard against.
- [x] **Ownership**: `requests`, `python-dotenv`, `pytest`, `pytest-mock`, existing Google Drive tooling — all already-approved, open-source, no new proprietary lock-in. Client owns all code.
- [x] **TDD**: Tests written alongside implementation, never after. Every new/extended tool/script gets unit tests; the approve → both-platforms-enqueued path gets an integration test. The reused prototype already ships 12 passing tests for the upload/publish path — these are extended, not discarded.
- [x] **Token safety**: No new long-lived secret is introduced beyond `IG_BUSINESS_ACCOUNT_ID` (not a secret — a public account identifier). `FB_PAGE_ACCESS_TOKEN` continues to be used only where Feature 003 already uses it; no new token-handling surface.
- [x] **Client scope isolation**: Instagram publishing is gated on a per-client `IG_BUSINESS_ACCOUNT_ID` being present in that client's `.env`. `_construction_co` has none, so its pipeline exercises no Instagram code path at all (FR-016) — matches its scoped-pipeline constitution.

---

## Technical Context

**Language/Version:** Python 3.11+
**Primary dependencies:** `requests`, `python-dotenv` (no new packages needed)
**Implementation root:** `platform/photo-agent/` (tools/scripts/tests — shared, post-migration); `clients/_demo/src/photo-agent/.env` for the one new client-specific var
**Storage:** `$FIELDKIT_DATA_DIR/photo-agent/instagram_state.json` — resolved via the `FIELDKIT_DATA_DIR` env var (e.g. `clients/_demo/data/photo-agent/instagram_state.json` for `_demo`), new file, same directory as `state.json` / `facebook_state.json`
**Logging:** `$FIELDKIT_LOG_DIR/photo-agent.log` — resolved via `FIELDKIT_LOG_DIR` (e.g. `clients/_demo/logs/photo-agent.log`), extended with new Instagram event types
**Testing:** pytest + pytest-mock, all tests live flat in `platform/photo-agent/tests/`, named `test_<module>.py`, HTTP/subprocess/filesystem mocked via `mocker.patch(...)` on module-qualified names — same convention as `test_facebook_api.py` et al.
**Target platform:** macOS (Mac Mini M-series), cloud-migration path tracked separately (see root constitution note on the Mac Mini → Cloud pivot; unaffected by this feature)
**Project type:** Cron job (`upload_instagram.py`) + one-shot admin CLI (`check_instagram_connection.py`)
**Instagram API:** Graph API v25.0, `graph.facebook.com` (confirmed — matches the existing prototype's `_GRAPH_BASE = "https://graph.facebook.com/v25.0"`)

---

## Implementation Phases

### Phase 0: Research

**Status: COMPLETE** — see `research.md`, revised per the Revision Note above.

Key decisions: Instagram Graph API's video/Reels publish is async and container-based (create → poll → publish), unlike Facebook's single-call multipart upload; it requires a public `video_url`, satisfied via a short-lived Drive share link on the already-stripped approved video (net-new — the existing prototype has no Drive logic); the Instagram professional account is discovered from the existing Facebook Page connection rather than through a new OAuth flow (also net-new); state management mirrors `facebook_state.py`'s actual current claim-based API, not a simplified reinvention; the existing orphaned `instagram_api.py` prototype (branch `feature/instagram-api-client-sk`) is cherry-picked and extended rather than rewritten.

### Phase 1: Core Implementation

Build new tools and scripts, extending the existing prototype where one exists, with full unit tests:

1. **`tools/instagram_api.py`** — Graph API wrapper. **Start by cherry-picking commit `03d938f` from `feature/instagram-api-client-sk`**, which already implements the container create → poll → publish flow as `upload_reel(access_token, ig_user_id, video_url) -> str` with private helpers `_create_container`, `_poll_container_status`, `_publish_container`. Then close these confirmed gaps:
   - Add `InstagramTokenError(RuntimeError)` and make `_create_container`/`_poll_container_status`/`_publish_container` raise it on Graph API error code 190 (mirroring `facebook_api.py`'s `FacebookTokenError` handling exactly) — the prototype currently funnels every error, including a hypothetical 190, into the single generic `InstagramUploadError`. This is a confirmed gap, not a design choice.
   - De-privatize the three helpers into the public contract this plan/`contracts/cli-contracts.md` specifies: `create_media_container(page_access_token, ig_user_id, video_url) -> str`, `get_container_status(page_access_token, container_id) -> str`, `publish_container(page_access_token, ig_user_id, container_id) -> str` (rename from the `_`-prefixed private forms; keep or wrap `upload_reel()` if useful, but the individually-testable public functions are required).
   - Add `discover_business_account(page_access_token, page_id) -> dict` (returns `{"id", "username", "account_type"}`) and `InstagramAccountNotFoundError(RuntimeError)` — entirely new, not in the prototype at all.
   - Keep the prototype's poll cap as-is (`_POLL_INTERVAL_SECONDS = 5`, `_MAX_POLL_ATTEMPTS = 60` → 300s total) — this plan has been updated to match rather than the reverse.

2. `tools/instagram_state.py` — `instagram_state.json` manager. **Mirror `tools/facebook_state.py`'s current public API exactly** (read that file's source first): `get_pending_upload()`, `set_pending_upload(record)`, `claim_pending_upload(idempotency_key, *, cooldown_seconds, max_attempts, lease_seconds) -> str`, `release_claim(idempotency_key)`, `clear_pending_upload(expected_idempotency_key) -> bool`, `mark_published(idempotency_key, post_id)`, `mark_failed(idempotency_key)`, `is_published(idempotency_key) -> bool`, `find_published(project_name) -> dict | None`. Adapted record keys: `project_name`, `video_local_path`, `ig_business_account_id` (not `page_id`), `status`, `attempt_count`, `last_attempt_at`, `triggered_at`, `idempotency_key`, `container_id` (Instagram-specific — cleared when an attempt concludes), `ig_post_id` (not `fb_post_id`). Same `fcntl.LOCK_EX` pattern; raises plain `ValueError`/`RuntimeError` like `facebook_state.py` (no custom exception classes).

3. `tools/instagram_logger.py` — Activity log events for Feature 005, same pipe-delimited format as `facebook_logger.py`, writing to `$FIELDKIT_LOG_DIR/photo-agent.log`
   - `log_upload_enqueued()`, `log_upload_started()`, `log_container_created()`, `log_container_ready()`, `log_upload_published()`, `log_upload_attempt_failed()`, `log_upload_exhausted()`, `log_token_expired()`

4. `tools/drive.py` extensions — `create_temporary_share_link(video_path) -> str` and `revoke_share_link(file_id) -> None`. **Entirely net-new** — confirmed the current `tools/drive.py` (on both `main` and the prototype branch) has zero share-link/hosting logic; only reuses the existing Drive client/auth already configured for Feature 002's client-initiated uploads.

5. `scripts/check_instagram_connection.py` — One-time admin CLI, net-new, using the same `FIELDKIT_ROOT`/`CLIENT_NAME` boilerplate as the other entrypoint scripts. Calls `instagram_api.discover_business_account()`; on success writes `IG_BUSINESS_ACCOUNT_ID` to the client's `.env` and prints a confirmation; on failure prints a clear, actionable message per FR-005.

6. `scripts/upload_instagram.py` — Cron upload script, net-new, modeled directly on the CURRENT `upload_facebook.py` (its own `upload_instagram.lock` re-entrancy guard, `claim_pending_upload`-based state machine — not a hand-rolled cooldown check). Reads pending job via `claim_pending_upload`, creates a temporary Drive share link, calls the `instagram_api` container flow, revokes the share link, handles retry/failure paths via `mark_failed`/`release_claim`, sends Telegram notifications.

**Output:** `tools/instagram_api.py` (extended from the cherry-picked prototype), `tools/instagram_state.py`, `tools/instagram_logger.py`, `tools/drive.py` (extended), `scripts/check_instagram_connection.py`, `scripts/upload_instagram.py` — all under `platform/photo-agent/` — plus unit tests `tests/test_instagram_api.py` (extended from the prototype's 12 tests), `tests/test_instagram_state.py`, `tests/test_instagram_logger.py`, `tests/test_check_instagram_connection.py`, `tests/test_upload_instagram.py`, and extensions to `tests/test_drive.py`.

### Phase 2: Integration

1. Extend `platform/photo-agent/scripts/check_approval.py` approve path:
   - Import `instagram_state`
   - After the existing `facebook_state.set_pending_upload()` call: if `IG_BUSINESS_ACCOUNT_ID` is set for the client, call `instagram_state.set_pending_upload()`, gated by the same idempotency check pattern already used for the Facebook enqueue
   - Failure to enqueue is logged as error but does NOT abort the existing approve flow or the Facebook enqueue

2. Add `upload_instagram.py` to the cron schedule (operational step, same cadence as `upload_facebook.py` — no crontab file is checked into the repo; this is a deployment-time step, consistent with how `upload_facebook.py`'s own cron installation is handled)

3. Add `IG_BUSINESS_ACCOUNT_ID` to `clients/_demo/src/photo-agent/.env.example`

4. Integration test: approve a video → verify both `facebook_state.json` and `instagram_state.json` transition → verify each platform's Telegram confirmation mock is called independently, and that a simulated Instagram-only failure does not affect the Facebook job's success

**Output:** Updated `check_approval.py`, `.env.example`, integration tests

---

## Project Structure

```text
platform/photo-agent/
├── scripts/
│   ├── check_approval.py            ← modified: also enqueues IG upload on approve
│   ├── check_instagram_connection.py ← NEW: one-time admin discovery CLI
│   ├── generate_auth_link.py        ← unchanged (Feature 003, reused for auth)
│   ├── process_photos.py            ← unchanged
│   ├── upload_facebook.py           ← unchanged
│   └── upload_instagram.py          ← NEW: cron upload script
├── tools/
│   ├── drive.py                     ← extended: temporary share-link helpers added
│   ├── facebook_api.py              ← unchanged
│   ├── facebook_logger.py           ← unchanged
│   ├── facebook_state.py            ← unchanged (its API is mirrored, not modified)
│   ├── instagram_api.py             ← EXTENDED from orphaned prototype (branch feature/instagram-api-client-sk, commit 03d938f)
│   ├── instagram_logger.py          ← NEW: activity log events for Feature 005
│   ├── instagram_state.py           ← NEW: instagram_state.json manager
│   ├── logger.py                    ← unchanged
│   ├── paths.py                     ← unchanged
│   ├── state.py                     ← unchanged
│   ├── telegram_api.py              ← unchanged
│   └── video_generator.py           ← unchanged
└── tests/
    ├── test_check_approval.py       ← extended: new IG enqueue assertions
    ├── test_check_instagram_connection.py ← NEW
    ├── test_instagram_api.py        ← EXTENDED from orphaned prototype's 12 tests
    ├── test_instagram_logger.py     ← NEW
    ├── test_instagram_state.py      ← NEW
    ├── test_drive.py                ← extended: share-link helper tests
    └── test_upload_instagram.py     ← NEW

clients/_demo/src/photo-agent/
├── .env                              ← modified: IG_BUSINESS_ACCOUNT_ID added (by check_instagram_connection.py)
└── .env.example                      ← modified: IG_BUSINESS_ACCOUNT_ID documented
```

**Structure Decision**: Mirrors the CURRENT (post-Platform-002-migration) `platform/photo-agent/` layout exactly — shared code, per-client `.env` only. This corrects the original draft, which by-analogy assumed Feature 003's pre-migration `clients/_demo/src/photo-agent/{tools,scripts,tests}/` layout.

---

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Upload trigger | `check_approval.py` enqueues both platforms; each cron drains its own queue | Non-blocking approval; retry logic stays local to each platform's script (FR-013) |
| Reuse vs. rewrite for `instagram_api.py` | Cherry-pick and extend the orphaned `feature/instagram-api-client-sk` prototype | It's unclaimed (no PR/issue references it), already implements the hard part (container create/poll/publish) correctly, and ships 12 passing tests — rewriting from scratch would throw away working, tested code for no benefit |
| Account auth | Reuse `FB_PAGE_ACCESS_TOKEN`; no new OAuth flow | Instagram Graph API access is inherent to a Page-linked Business/Creator account; a second auth flow would duplicate Feature 003 for no benefit |
| Video hosting for container creation | Short-lived Drive share link, revoked after publish/final-failure | Only way to satisfy Instagram Graph API's `video_url` requirement without standing up a public web server on the Mac Mini; confirmed net-new work, not present in the prototype |
| State isolation & API shape | Separate `instagram_state.json`, mirroring `facebook_state.py`'s CURRENT claim-based API (`claim_pending_upload`/`release_claim`/`clear_pending_upload`/`mark_published`/`mark_failed`/`find_published`) | Facebook and Instagram jobs for the same video must never block each other (FR-013); reusing the exact current pattern (not an outdated simpler one) keeps the two platform integrations genuinely symmetric with what's actually deployed |
| Error classification | `InstagramTokenError` (skip retries, error code 190) vs `InstagramUploadError` (retry) vs `InstagramAccountNotFoundError` (connection-check only) | Mirrors `facebook_api.py`'s `FacebookTokenError` / `FacebookUploadError` split; `InstagramTokenError` and `InstagramAccountNotFoundError` are confirmed absent from the prototype and must be added |
| Duplicate prevention | Same idempotency key already used for the Facebook job (Telegram `message_id`) | Already unique per approval event; reused, not reinvented |
| Container polling bound | 5s interval, **300s (5 min) cap** — matches the prototype as-implemented | Originally specified as 3 minutes; changed to match the existing tested implementation rather than force a rework for no functional gain |
| Cron re-entrancy | `upload_instagram.lock`, mirroring `upload_facebook.lock` | `upload_facebook.py`'s docstring documents this exact pattern as the guard against overlapping cron invocations; Instagram's cron script needs the same guard |
| Logging | Extend `photo-agent.log` via `$FIELDKIT_LOG_DIR` | Single per-client log per spec (FR-012); same pattern as Feature 002/003 |

---

## Open Questions

- None — all spec ambiguities were resolved with documented Assumptions in `spec.md`, directly modeled on Feature 003 precedent, and the implementation-detail questions raised by the post-migration code layout are resolved above.

**Outside-code prerequisites** (non-blocking for coding):
- Admin links the client's Instagram account to the client's Facebook Page in Meta's settings, and converts it to a Business or Creator account if it is not already (`check_instagram_connection.py` will fail clearly and tell the admin this is needed if it isn't done yet).
- No new Meta Developer App configuration is required beyond what Feature 003 already set up — Instagram publishing is granted by the same Page permissions.
- Cron installation for `upload_instagram.py` (crontab entry) is a deployment-time step, same as `upload_facebook.py`'s — not tracked as a repo change.
