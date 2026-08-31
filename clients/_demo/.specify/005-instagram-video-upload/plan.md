# 005 — Instagram Video Upload: Technical Plan

**Status:** Technical Planning
**Spec:** [`spec.md`](spec.md)
**Sequence diagram:** [`sequence-diagram.md`](sequence-diagram.md)
**Last Updated:** 2026-08-31

---

## Summary

Extend the `_demo` pipeline so that the single Telegram approval which already triggers a Facebook post (Feature 003) also triggers an Instagram Reel publish, via the Instagram Graph API. Instagram publishing is authenticated through the Facebook Page access token already stored for Feature 003 — no new OAuth flow — and the two platform uploads are independent queues, jobs, and failure domains, mirroring Feature 003's architecture as closely as possible.

---

## Stack

| Concern | Solution | Rationale |
|---------|----------|-----------|
| Instagram API client | `requests` — direct Graph API v25.0 (`graph.facebook.com`, Instagram is served from the same Graph host) | Already a dependency; consistent with `facebook_api.py`; no SDK needed |
| Account discovery | `GET /{page_id}?fields=instagram_business_account` using the existing FB Page token | Reuses Feature 003 credential; zero new OAuth surface |
| Video source for publish | Temporary Google Drive shareable link to the already-approved, already-metadata-stripped video file | Instagram Graph API's media-container creation requires a publicly reachable `video_url`, unlike Facebook's direct multipart upload; Drive is the framework's existing sanctioned exception for hosting client-approved media (root constitution, Architecture Constraints) |
| Publish flow | Two-step async: `POST /{ig_user_id}/media` (create container) → poll `GET /{container_id}?fields=status_code` → `POST /{ig_user_id}/media_publish` | Required by Instagram Graph API for video/Reels; Facebook's single-call upload does not apply here |
| State persistence | `instagram_state.json` + `fcntl` locking | Mirrors `facebook_state.py` / `state.py` pattern exactly; kept separate from `facebook_state.json` so the two platforms' jobs never block each other (FR-013) |
| Retry logic | Timestamp-based cooldown in cron script | Matches `upload_facebook.py` cadence; no threads |
| Token storage | `.env` — reuses `FB_PAGE_ACCESS_TOKEN`; adds `IG_BUSINESS_ACCOUNT_ID` only | Consistent with existing credential pattern; no new secret class |
| Logging | `instagram_logger.py` → `photo-agent.log` | Single per-client log per spec (FR-012), same file as Feature 002/003 |
| Container polling timeout | Poll every 5s, cap at 3 minutes total, then treat as a retryable failure | Bounds the "stuck container" edge case identified in spec.md without introducing unbounded waits |

---

## Architecture

Feature 005 follows the same two-script cron pattern as Feature 003, kept as a parallel, independent track rather than woven into the Facebook code path. `check_approval.py` gains a second enqueue call on the approve path (alongside the existing `facebook_state.set_pending_upload()` call): `instagram_state.set_pending_upload()`. A new cron script, `upload_instagram.py`, runs on the same cadence as `upload_facebook.py` and drains the Instagram queue independently — creating a temporary Drive share link for the approved video, creating an Instagram media container, polling until Instagram finishes processing it, publishing it, and sending a Telegram confirmation. Failures, retries, and token-expiry handling are scoped to the Instagram job only (FR-013): a Facebook failure never blocks or retries the Instagram job and vice versa. A one-time admin CLI script, `check_instagram_connection.py`, discovers the Instagram professional account linked to the client's already-connected Facebook Page and writes its ID to `.env` — no new OAuth dance, since Instagram Graph API access rides on the Page token FieldKit already holds from Feature 003.

---

## Constitution Check

*All gates must pass before implementation begins.*

- [x] **Privacy**: The video published to Instagram is the same already-approved, already-metadata-stripped asset used for the Facebook post (FR-014) — no re-processing, no new metadata exposure. The temporary Drive share link used only to satisfy Instagram's `video_url` requirement is scoped to the single approved video, created immediately before the container call and revoked immediately after publish (or after final failure) so it is not left reachable.
- [x] **HITL**: No second approval gate is introduced — Instagram publishing rides the same Telegram approval tap that already gates Facebook publishing (FR-002). FieldKit never publishes to Instagram without that prior human approval.
- [x] **Budget**: No AI/LLM API calls in this feature — same as Feature 003. The only external calls are to the Instagram Graph API (free for publishing) and Google Drive API (already used, free tier) and Telegram Bot API (free). FR-015's pause-on-exhaustion requirement is satisfied by the pipeline's existing shared budget-guard check (already invoked before any per-client cron work runs); this feature adds no new AI spend to guard against.
- [x] **Ownership**: `requests`, `python-dotenv`, `pytest`, `pytest-mock`, existing Google Drive tooling — all already-approved, open-source, no new proprietary lock-in. Client owns all code.
- [x] **TDD**: Tests written alongside implementation, never after. Every new tool/script gets unit tests; the approve → both-platforms-enqueued path gets an integration test.
- [x] **Token safety**: No new long-lived secret is introduced beyond `IG_BUSINESS_ACCOUNT_ID` (not a secret — a public account identifier). `FB_PAGE_ACCESS_TOKEN` continues to be used only where Feature 003 already uses it; no new token-handling surface.
- [x] **Client scope isolation**: Instagram publishing is gated on a per-client `IG_BUSINESS_ACCOUNT_ID` being present in that client's `.env`. `_construction_co` has none, so its pipeline exercises no Instagram code path at all (FR-016) — matches its scoped-pipeline constitution.

---

## Technical Context

**Language/Version:** Python 3.11+
**Primary dependencies:** `requests`, `python-dotenv` (no new packages needed)
**Storage:** `data/photo-agent/instagram_state.json` — new file, same directory as `state.json` / `facebook_state.json`
**Logging:** `logs/photo-agent.log` — existing per-client log, extended with new Instagram event types
**Testing:** pytest + pytest-mock
**Target platform:** macOS (Mac Mini M-series), cloud-migration path tracked separately (see root constitution note on the Mac Mini → Cloud pivot; unaffected by this feature)
**Project type:** Cron job (`upload_instagram.py`) + one-shot admin CLI (`check_instagram_connection.py`)
**Instagram API:** Graph API v25.0, `graph.facebook.com` (Instagram publishing is served from the Facebook Graph host, not a separate Instagram-only API)

---

## Implementation Phases

### Phase 0: Research

**Status: COMPLETE**

Key decisions: Instagram Graph API's video/Reels publish is async and container-based (create → poll → publish), unlike Facebook's single-call multipart upload; it requires a public `video_url`, satisfied via a short-lived Drive share link on the already-stripped approved video; the Instagram professional account is discovered from the existing Facebook Page connection rather than through a new OAuth flow, since a Business/Creator Instagram account must already be linked to a Facebook Page for Graph API access to work at all.

### Phase 1: Core Implementation

Build new tools and scripts in isolation, with full unit tests:

1. `tools/instagram_api.py` — Graph API wrapper
   - `discover_business_account(page_token, page_id)`, `create_media_container(token, ig_user_id, video_url)`, `get_container_status(token, container_id)`, `publish_container(token, ig_user_id, container_id)`
   - Custom exceptions: `InstagramTokenError` (irrecoverable — mirrors `FacebookTokenError`), `InstagramUploadError` (retryable), `InstagramAccountNotFoundError` (used by the connection check, not the cron path)

2. `tools/instagram_state.py` — `instagram_state.json` manager
   - `get_pending_upload()`, `set_pending_upload()`, `mark_uploading()`, `mark_published()`, `mark_failed()`, `increment_attempt()`, `is_published()`
   - Same `fcntl.LOCK_EX` pattern as `facebook_state.py`

3. `tools/instagram_logger.py` — Activity log events for Feature 005
   - `log_upload_enqueued()`, `log_upload_started()`, `log_container_created()`, `log_container_ready()`, `log_upload_published()`, `log_upload_attempt_failed()`, `log_upload_exhausted()`, `log_token_expired()`
   - Writes to the same `photo-agent.log`

4. `scripts/check_instagram_connection.py` — One-time admin CLI
   - Calls `instagram_api.discover_business_account()` against the client's existing FB Page token; on success writes `IG_BUSINESS_ACCOUNT_ID` to `.env` and prints a confirmation; on failure prints a clear, actionable message per FR-005 (no linked account found / account is not Business or Creator)

5. `scripts/upload_instagram.py` — Cron upload script
   - Reads pending job, checks cooldown, creates a temporary Drive share link for the video, calls `instagram_api.create_media_container()`, polls `get_container_status()` (5s interval, 3-minute cap), calls `publish_container()`, revokes the Drive share link, handles retry and failure paths, sends Telegram notifications — entirely independent of `upload_facebook.py`'s job for the same video

**Output:** All five files + unit tests (`test_instagram_api.py`, `test_instagram_state.py`, `test_instagram_logger.py`, `test_check_instagram_connection.py`, `test_upload_instagram.py`)

### Phase 2: Integration

1. Extend `check_approval.py` approve path:
   - Import `instagram_state`
   - After the existing `facebook_state.set_pending_upload()` call: call `instagram_state.set_pending_upload()`, gated on `IG_BUSINESS_ACCOUNT_ID` being present for the client (skip silently if not configured, per FR-016)
   - Idempotency check before setting: if `is_published(idempotency_key)` → skip silently, mirroring the Facebook path

2. Add `upload_instagram.py` to the cron schedule (same cadence as `upload_facebook.py`)

3. Add `IG_BUSINESS_ACCOUNT_ID` to `.env.example`

4. Integration test: approve a video → verify both `facebook_state.json` and `instagram_state.json` transition → verify each platform's Telegram confirmation mock is called independently, and that a simulated Instagram-only failure does not affect the Facebook job's success

**Output:** Updated `check_approval.py`, `cron` config, `.env.example`, integration tests

---

## Project Structure

```text
clients/_demo/src/photo-agent/
├── scripts/
│   ├── check_approval.py            ← modified: also enqueues IG upload on approve
│   ├── check_instagram_connection.py ← NEW: one-time admin discovery CLI
│   ├── generate_auth_link.py        ← unchanged (Feature 003, reused for auth)
│   ├── process_photos.py            ← unchanged
│   ├── upload_facebook.py           ← unchanged
│   └── upload_instagram.py          ← NEW: cron upload script
├── tools/
│   ├── drive.py                     ← reused: temporary share-link helper added
│   ├── facebook_api.py              ← unchanged
│   ├── facebook_logger.py           ← unchanged
│   ├── facebook_state.py            ← unchanged
│   ├── instagram_api.py             ← NEW: Graph API wrapper
│   ├── instagram_logger.py          ← NEW: activity log events for Feature 005
│   ├── instagram_state.py           ← NEW: instagram_state.json manager
│   ├── logger.py                    ← unchanged
│   ├── state.py                     ← unchanged
│   ├── telegram_api.py              ← unchanged
│   └── video_generator.py           ← unchanged
└── tests/
    ├── test_check_approval.py       ← extended: new IG enqueue assertions
    ├── test_check_instagram_connection.py ← NEW
    ├── test_instagram_api.py        ← NEW
    ├── test_instagram_logger.py     ← NEW
    ├── test_instagram_state.py      ← NEW
    └── test_upload_instagram.py     ← NEW
```

**Structure Decision**: Mirrors Feature 003's file layout exactly (same directories, same one-tool-per-concern split), so the two platform integrations read as parallel, symmetric modules rather than a single tangled multi-platform uploader.

---

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Upload trigger | `check_approval.py` enqueues both platforms; each cron drains its own queue | Non-blocking approval; retry logic stays local to each platform's script (FR-013) |
| Account auth | Reuse `FB_PAGE_ACCESS_TOKEN`; no new OAuth flow | Instagram Graph API access is inherent to a Page-linked Business/Creator account; a second auth flow would duplicate Feature 003 for no benefit |
| Video hosting for container creation | Short-lived Drive share link, revoked after publish/final-failure | Only way to satisfy Instagram Graph API's `video_url` requirement without standing up a public web server on the Mac Mini |
| State isolation | Separate `instagram_state.json` | Facebook and Instagram jobs for the same video must never block each other (FR-013); clean rollback boundary, same as Feature 002/003 kept separate |
| Error classification | `InstagramTokenError` (skip retries) vs `InstagramUploadError` (retry) | Mirrors Feature 003's `FacebookTokenError` / `FacebookUploadError` split |
| Duplicate prevention | Same idempotency key already used for the Facebook job (Telegram `message_id`) | Already unique per approval event; reused, not reinvented |
| Container polling bound | 5s interval, 3-minute cap | Bounds the "stuck container" edge case from spec.md; treated as a retryable `InstagramUploadError` on timeout |
| Logging | Extend `photo-agent.log` | Single per-client log per spec (FR-012); same pattern as Feature 002/003 |

---

## Open Questions

- None — all spec ambiguities were resolved with documented Assumptions in `spec.md`, directly modeled on Feature 003 precedent.

**Outside-code prerequisites** (non-blocking for coding):
- Admin links the client's Instagram account to the client's Facebook Page in Meta's settings, and converts it to a Business or Creator account if it is not already (`check_instagram_connection.py` will fail clearly and tell the admin this is needed if it isn't done yet).
- No new Meta Developer App configuration is required beyond what Feature 003 already set up — Instagram publishing is granted by the same Page permissions.
