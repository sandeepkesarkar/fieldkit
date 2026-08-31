# Feature Specification: Instagram Video Upload

**Feature Branch**: `002-instagram-video-upload`

**Created**: 2026-08-31

**Status**: Draft

**Input**: User description: "Instagram Video Upload — add an Instagram upload step to the _demo client's photo/video agent pipeline. Today, once a generated site video is approved by the human via Telegram, the pipeline uploads it to Facebook (see clients/_demo/.specify/003-facebook-upload). This feature adds a parallel/sequential Instagram upload of that same approved video (as a Reel/feed video post) via the Instagram Graph API, reusing the existing Telegram human-in-the-loop approval gate (no separate approval step) and the existing per-client credential/config pattern. Must comply with the root FieldKit constitution (privacy, HITL, budget governance) and the _demo client constitution. Scope is _demo only for now (_construction_co does not get Facebook or Instagram upload per its scoped pipeline)."

---

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Approved Video Auto-Posts to Instagram (Priority: P1)

When a small business owner approves a generated video via Telegram, FieldKit automatically uploads and publishes it as a Reel on the owner's linked Instagram professional account — no extra approval step required beyond the single Telegram approval that already triggers the Facebook post. The owner receives a Telegram confirmation once the Instagram post is live.

**Why this priority**: This is the core value of the feature — extending the existing "approve once, publish everywhere" flow to a second, high-reach platform without adding friction for the owner.

**Independent Test**: Can be fully tested by approving a video in Telegram and verifying that a Reel appears on the linked Instagram account within a reasonable time, and a confirmation message arrives in Telegram.

**Acceptance Scenarios**:

1. **Given** a video has been approved via Telegram, **When** the approval is processed, **Then** FieldKit uploads the video to the linked Instagram account and publishes it as a Reel within 5 minutes.
2. **Given** a video has been published to Instagram, **When** the post goes live, **Then** the owner receives a Telegram message confirming the Instagram post with a direct link to it.
3. **Given** a single video approval, **When** the approval is processed, **Then** both the Facebook post (Feature 003) and the Instagram post are triggered from that one approval — the owner is never asked to approve the same video twice.
4. **Given** a video is approved, **When** no caption template is configured, **Then** the video is posted with no caption text (caption-only content is out of scope for this feature, matching Feature 003).

---

### User Story 2 — Instagram Account Connection Setup (Priority: P2)

A small business owner connects their Instagram professional account to FieldKit once, via the same Facebook Page authorization already used for Feature 003. After this one-time setup, FieldKit can publish to their Instagram account automatically without requiring the owner to log in again.

**Why this priority**: Without a connected Instagram account, no posts can be made. However, this is a one-time setup task that piggybacks on existing infrastructure, not a recurring user action, making the approval flow (P1) the higher-priority story for feature completeness.

**Independent Test**: Can be tested independently by running the connection check against a client whose Facebook Page has (and separately, has not) got a linked Instagram professional account, and verifying FieldKit correctly stores or rejects the connection.

**Acceptance Scenarios**:

1. **Given** a client's Facebook Page (connected under Feature 003) has a linked Instagram professional account, **When** the administrator runs the Instagram connection check, **Then** FieldKit discovers and stores the Instagram account identifier needed to publish on its behalf, and the owner sees a confirmation that their Instagram account is linked.
2. **Given** a client's Facebook Page has no linked Instagram professional account, **When** the administrator runs the Instagram connection check, **Then** FieldKit reports clearly that no Instagram account was found and that one must be linked to the Page in Meta's settings before this feature can be enabled for that client.
3. **Given** the owner's Instagram account is a personal (non-professional) account, **When** the connection check runs, **Then** FieldKit reports that a Business or Creator account is required and does not attempt to publish.

---

### User Story 3 — Upload Failure Recovery (Priority: P3)

If an Instagram upload fails (expired token, API error, network issue, video processing rejected by Instagram), FieldKit retries automatically and alerts the owner via Telegram if all retries are exhausted — independently of whatever happens to the Facebook post for the same video.

**Why this priority**: Failure recovery is critical for reliability but does not deliver primary user value on its own — it protects the P1 scenario rather than enabling it.

**Independent Test**: Can be tested independently by simulating an Instagram API failure and verifying that retry attempts occur and a Telegram alert is sent after retries are exhausted, while a healthy Facebook upload for the same video is unaffected.

**Acceptance Scenarios**:

1. **Given** an Instagram video upload fails due to a transient error, **When** the failure is detected, **Then** FieldKit retries the upload up to 3 times with a fixed 60-second delay between each attempt.
2. **Given** all retry attempts fail, **When** the final attempt fails, **Then** FieldKit sends a Telegram alert notifying the owner that the Instagram upload failed and requires attention.
3. **Given** a retry attempt succeeds after one or more failures, **When** the upload completes, **Then** the owner receives the normal success confirmation and no failure alert is sent.
4. **Given** an Instagram access token has expired, **When** an upload is attempted, **Then** FieldKit detects the expiry, skips retries (retrying won't help), and alerts the owner to reconnect their account.
5. **Given** the Facebook upload for a video fails while the Instagram upload for the same video succeeds (or vice versa), **When** either outcome occurs, **Then** each platform's success or failure is reported to the owner independently, with no cross-platform blocking.

---

### Edge Cases

- What happens when the linked Instagram account is unpublished, restricted, or converted back to a personal account by Meta at time of posting?
- What happens if the approved video file is missing or corrupted when the Instagram upload is triggered?
- What if the owner revokes FieldKit's Instagram/Facebook permission after initial authorization?
- What if the Instagram API rate limit is hit (too many uploads in a short period)?
- What if the same video is approved twice (duplicate prevention)?
- What if the video's aspect ratio or duration falls outside what Instagram Reels accepts, even though it was accepted by Facebook?
- What happens if Instagram's asynchronous video-processing step never finishes (stuck container) — how long does FieldKit wait before treating it as a failure?

---

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST automatically upload and publish the approved video to the client's linked Instagram professional account as a Reel when a video approval event is received.
- **FR-002**: System MUST trigger the Instagram publish and the existing Facebook publish (Feature 003) from the same single Telegram approval event — the owner MUST NOT be asked for a second, Instagram-specific approval.
- **FR-003**: System MUST send a Telegram confirmation message to the owner after a successful Instagram post, including a direct link to the post.
- **FR-004**: System MUST support connecting exactly one Instagram professional (Business or Creator) account per client, discovered via that client's already-connected Facebook Page.
- **FR-005**: System MUST provide an administrator-run connection check that discovers the Instagram account linked to a client's Facebook Page and reports a clear, actionable message when no eligible (Business/Creator) Instagram account is found.
- **FR-006**: System MUST store the client's Instagram-related identifiers alongside the existing Facebook Page credentials in the client's `.env` file, consistent with FieldKit's established credential management pattern.
- **FR-007**: System MUST retry a failed Instagram upload up to 3 times with a fixed 60-second delay between attempts before treating it as a permanent failure.
- **FR-008**: System MUST detect expired Instagram/Facebook access credentials and alert the owner via Telegram rather than retrying indefinitely.
- **FR-009**: System MUST send a Telegram alert to the owner when an Instagram video upload fails after all retry attempts are exhausted.
- **FR-010**: System MUST post videos with no caption text until a caption generation feature is available, matching Feature 003's behavior.
- **FR-011**: System MUST prevent duplicate Instagram posts if the same approved video triggers the upload flow more than once.
- **FR-012**: System MUST log all Instagram upload attempts, outcomes (published/failed), retry counts, and error details to the client's per-client log file for operational visibility, consistent with Feature 003's logging.
- **FR-013**: System MUST treat the Instagram publish and the Facebook publish for a given approved video as independent outcomes — a failure on one platform MUST NOT block, retry, or roll back the other platform's post.
- **FR-014**: System MUST strip all metadata (GPS, timestamps, camera info, faces) from the video before it is uploaded to Instagram, per the framework privacy gate — reusing the same stripped asset already produced for the Facebook upload rather than re-processing.
- **FR-015**: System MUST enforce the client's daily AI/API budget limit and pause Instagram publishing operations (queuing them for the next budget window) if the client's daily budget has already been exhausted, consistent with framework budget governance.
- **FR-016**: System MUST only enable Instagram publishing for clients explicitly configured for it; clients without an Instagram connection (e.g., `_construction_co`, which is out of scope for this feature) MUST see no Instagram-related behavior at all.

### Key Entities

- **InstagramAccountConnection**: Represents a client's linked Instagram professional account — stores the Instagram account identifier, the Facebook Page it is linked through, and the access credential needed to publish on its behalf. One per client, reusing the client's existing FacebookPageConnection (Feature 003).
- **InstagramUploadJob**: Represents a single Instagram upload attempt for an approved video — tracks the video reference, target Instagram account, attempt count, and timestamps. Moves through states: `pending → uploading → published / failed`, independent of that same video's Facebook upload job.
- **UploadAttempt**: Represents one individual retry within an InstagramUploadJob — records the outcome and any error detail returned by Instagram.

---

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An approved video appears as a live Reel on the linked Instagram account within 5 minutes of the approval event.
- **SC-002**: The owner receives a Telegram confirmation with a post link within 30 seconds of the video going live on Instagram.
- **SC-003**: Upload failures due to transient errors are recovered automatically without owner intervention at least 90% of the time.
- **SC-004**: When an upload cannot be recovered, the owner is alerted via Telegram within 5 minutes of the final failed attempt.
- **SC-005**: The one-time Instagram connection check can be completed by the administrator in under 5 minutes, without requiring the business owner to take any separate action beyond what Feature 003 already required.
- **SC-006**: No video is posted to Instagram more than once, even if the approval event is received multiple times.
- **SC-007**: A single Telegram approval reliably results in both the Facebook and Instagram posts being attempted, with each platform's outcome reported to the owner independently of the other.

---

## Assumptions

- Each client (e.g., `_demo`) has at most one Instagram professional (Business or Creator) account, linked to the same single Facebook Page connected under Feature 003; multi-account support is out of scope.
- Instagram publishing reuses the Facebook Page connection and administrator-driven authorization flow already built for Feature 003 — no new, separate OAuth setup story is introduced for Instagram; only a connection *check* (FR-005) that discovers the linked Instagram account is added.
- The administrator (Sandeep) performs the one-time Instagram connection check; the business owner does not interact with Meta's developer portal.
- Caption text is out of scope; this feature posts video-only, matching Feature 003. Caption generation is a separate future feature.
- The video file produced by Feature 002 (photo/video agent) and already validated for Facebook (9:16 portrait, MP4) is assumed compatible with Instagram Reels without additional conversion; if Instagram-specific constraints (duration, encoding) differ, that is a technical/planning concern, not a scope change.
- Facebook and Instagram publishing for the same approved video are independent operations that may run in parallel or in sequence at the implementation's discretion — the user-facing contract is that both are attempted from one approval and neither blocks the other.
- Post scheduling (choosing a future publish time) is out of scope for this feature; all posts go live immediately upon approval, matching Feature 003.
- The existing Telegram bot infrastructure from Features 001/002/003 is reused for all owner notifications.
- Instagram access credentials may expire and will need periodic renewal; alerting the owner is sufficient for this feature — automatic token refresh is out of scope.
- This feature applies to `_demo` only; `_construction_co`'s scoped pipeline (video → Telegram approval only) is explicitly unaffected and remains out of scope, per its existing constitution.
