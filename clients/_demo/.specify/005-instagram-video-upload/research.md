# Feature 005 — Research Findings

## Decision: Instagram Graph API version and host

**Choice**: v25.0, served from `graph.facebook.com` (not a separate `graph.instagram.com` host for this flow)
**Rationale**: Instagram content publishing for professional accounts is part of the Facebook Graph API, reached through the linked Facebook Page/Business object — consistent with the version already adopted in Feature 003.
**Alternatives considered**: Instagram Basic Display API — deprecated for this purpose and does not support publishing; not viable.

---

## Decision: Video publish flow — container create → poll → publish

**Choice**: `POST /{ig_user_id}/media` (create container, `media_type=REELS`, `video_url=...`) → poll `GET /{container_id}?fields=status_code` until `FINISHED` → `POST /{ig_user_id}/media_publish`
**Rationale**: Unlike Facebook's single-call multipart upload, Instagram Graph API ingests video asynchronously: the platform must first download and transcode the video from a URL before a container can be published. This three-step flow is the only supported mechanism for video/Reels publishing via the Graph API.
**Alternatives considered**: Direct binary upload (not supported by the Instagram content-publishing endpoints); Instagram's separate resumable "Content Publishing API" upload-by-file variant — more complex, not needed at our video sizes.

---

## Decision: Video source — temporary Google Drive share link

**Choice**: Generate a short-lived, viewer-only Google Drive share link for the already-approved, already-metadata-stripped video file immediately before container creation; revoke it immediately after the container reaches `FINISHED` (or after final failure).
**Rationale**: Instagram's container-creation call requires a publicly reachable `video_url` it can fetch from — there is no "upload bytes directly" option for this endpoint. The Mac Mini has no public web server. Google Drive is already the framework's sanctioned exception for hosting client-approved media (root constitution, Architecture Constraints), and Feature 002 already has Drive tooling in place.
**Alternatives considered**: Standing up a temporary public tunnel/HTTP server on the Mac Mini — more moving parts, more attack surface, no reuse of existing infrastructure; hosting on a permanent public bucket — unnecessary standing exposure for a one-time fetch.

---

## Decision: Account discovery instead of a new OAuth flow

**Choice**: `GET /{page_id}?fields=instagram_business_account` using the existing `FB_PAGE_ACCESS_TOKEN` from Feature 003.
**Rationale**: An Instagram account must already be converted to Business/Creator and linked to a Facebook Page for Graph API publishing to be possible at all. Given that constraint, the Page token FieldKit already holds is sufficient to discover and publish to the linked Instagram account — no additional permission scopes or OAuth round-trip needed.
**Alternatives considered**: A separate Instagram-specific OAuth flow (Instagram Login) — only relevant for the deprecated Basic Display API surface; not applicable to Business/Creator content publishing, and would duplicate Feature 003's setup for no benefit.

---

## Decision: Container polling strategy

**Choice**: Poll every 5 seconds, cap total wait at 3 minutes, then treat as a retryable `InstagramUploadError`.
**Rationale**: Instagram's processing time scales with video length/encoding but is typically well under a minute for our 20–60s portrait videos. A 3-minute cap bounds the "stuck container" edge case identified in `spec.md` without introducing unbounded waits inside the cron tick.
**Alternatives considered**: Webhook-based status notification — requires a public endpoint (same problem as video hosting); unnecessary complexity for demo scale.

---

## Decision: Retry strategy, duplicate prevention, state/log isolation

**Choice**: Identical mechanics to Feature 003 — timestamp-based cooldown check in the cron script, `str(telegram_message_id)` idempotency key, separate `instagram_state.json`, new `instagram_logger.py` writing into the shared `photo-agent.log`.
**Rationale**: Feature 003 already solved these problems; reusing the exact pattern keeps the two platform integrations symmetric and minimizes new surface area to review. Keeping state files separate (rather than one shared multi-platform state file) is what guarantees FR-013 (platform independence) is structurally true, not just tested for.
**Max attempts**: 3, same as Feature 003.

---

## Unresolved (non-blocking for implementation)

- **Instagram professional account linkage**: Admin must ensure the client's Instagram account is converted to Business/Creator and linked to the client's Facebook Page in Meta's account settings, before `check_instagram_connection.py` can succeed. Not a code dependency — `check_instagram_connection.py` fails with a clear, actionable message (FR-005) if this hasn't been done.
- **Reels-specific constraints (duration/aspect ratio) at scale**: Our current videos (9:16, 20–60s) are expected to satisfy Reels requirements based on public Instagram documentation; if a specific approved video is rejected by Instagram for format reasons, that surfaces as a normal `InstagramUploadError` and is out of scope to pre-validate in this feature.
