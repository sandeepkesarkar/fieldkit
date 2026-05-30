# Feature Specification: Facebook Video Upload

**Feature Branch**: `001-upload-facebook-video`

**Created**: 2026-05-30

**Status**: Draft

**Input**: Upload approved videos automatically to a small business owner's Facebook Page

---

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Approved Video Auto-Posts to Facebook (Priority: P1)

When a small business owner approves a generated video via Telegram, FieldKit automatically uploads and publishes it as a public post on the owner's linked Facebook Page — no extra step required. The owner receives a Telegram confirmation once the post is live.

**Why this priority**: This is the core value of the feature — closing the loop from content generation to social media distribution without any manual effort beyond a single approval tap.

**Independent Test**: Can be fully tested by approving a video in Telegram and verifying that a public post appears on the linked Facebook Page within a reasonable time, and a confirmation message arrives in Telegram.

**Acceptance Scenarios**:

1. **Given** a video has been approved via Telegram, **When** the approval is processed, **Then** FieldKit uploads the video to the linked Facebook Page and publishes it as a public post within 2 minutes.
2. **Given** a video has been published to Facebook, **When** the post goes live, **Then** the owner receives a Telegram message confirming the post with a direct link to it.
3. **Given** a video is approved, **When** no caption template is configured, **Then** the video is posted with no caption text (caption-only content is out of scope for this feature).

---

### User Story 2 — Facebook Page Connection Setup (Priority: P2)

A small business owner connects their Facebook Page to FieldKit once, via an authorization link. After this one-time setup, FieldKit can post to their Page automatically without requiring the owner to log in again.

**Why this priority**: Without a connected Page, no posts can be made. However, this is a one-time setup task, not a recurring user action, making the approval flow (P1) the higher-priority story for feature completeness.

**Independent Test**: Can be tested independently by generating an authorization link, completing the Facebook authorization flow, and verifying that FieldKit stores a valid connection for the client's Page.

**Acceptance Scenarios**:

1. **Given** a client is being onboarded, **When** the administrator generates a Facebook authorization link for that client, **Then** the link directs the owner to Facebook's authorization screen requesting Page posting permission.
2. **Given** the owner completes Facebook authorization, **When** the authorization succeeds, **Then** FieldKit stores the connection and the owner sees a confirmation that their Page is linked.
3. **Given** the owner has multiple Facebook Pages, **When** completing authorization, **Then** the owner can select which single Page to link to FieldKit.

---

### User Story 3 — Upload Failure Recovery (Priority: P3)

If a Facebook upload fails (expired token, API error, network issue), FieldKit retries automatically and alerts the owner via Telegram if all retries are exhausted.

**Why this priority**: Failure recovery is critical for reliability but does not deliver primary user value on its own — it protects the P1 scenario rather than enabling it.

**Independent Test**: Can be tested independently by simulating an API failure and verifying that retry attempts occur and a Telegram alert is sent after retries are exhausted.

**Acceptance Scenarios**:

1. **Given** a video upload fails due to a transient error, **When** the failure is detected, **Then** FieldKit retries the upload up to 3 times with a fixed 60-second delay between each attempt.
2. **Given** all retry attempts fail, **When** the final attempt fails, **Then** FieldKit sends a Telegram alert notifying the owner that the upload failed and requires attention.
3. **Given** a retry attempt succeeds after one or more failures, **When** the upload completes, **Then** the owner receives the normal success confirmation and no failure alert is sent.
4. **Given** a Facebook access token has expired, **When** an upload is attempted, **Then** FieldKit detects the expiry, skips retries (retrying won't help), and alerts the owner to reconnect their Facebook Page.

---

### Edge Cases

- What happens when the Facebook Page is unpublished or restricted by Meta at time of posting?
- What happens if the approved video file is missing or corrupted when the upload is triggered?
- What if the owner revokes FieldKit's Facebook permission after initial authorization?
- What if the Facebook API rate limit is hit (too many uploads in a short period)?
- What if the same video is approved twice (duplicate prevention)?

---

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST automatically upload and publish the approved video to the client's linked Facebook Page when a video approval event is received.
- **FR-002**: System MUST publish the video as a public post visible to anyone on Facebook.
- **FR-003**: System MUST send a Telegram confirmation message to the owner after a successful post, including a direct link to the Facebook post.
- **FR-004**: System MUST support connecting exactly one Facebook Page per client.
- **FR-005**: System MUST provide a CLI script that generates a Facebook authorization URL, allowing the administrator to share it with the Page owner to grant FieldKit posting permission via the standard Facebook OAuth flow.
- **FR-006**: System MUST store the client's Facebook Page access token in the client's `.env` file alongside other existing secrets, consistent with FieldKit's established credential management pattern.
- **FR-007**: System MUST retry a failed upload up to 3 times with a fixed 60-second delay between attempts before treating it as a permanent failure.
- **FR-008**: System MUST detect expired Facebook access credentials and alert the owner via Telegram rather than retrying indefinitely.
- **FR-009**: System MUST send a Telegram alert to the owner when a video upload fails after all retry attempts are exhausted.
- **FR-010**: System MUST post videos with no caption text until a caption generation feature is available.
- **FR-011**: System MUST prevent duplicate posts if the same approved video triggers the upload flow more than once.
- **FR-012**: System MUST log all upload attempts, outcomes (published/failed), retry counts, and error details to a per-client log file for operational visibility.

### Key Entities

- **FacebookPageConnection**: Represents a client's linked Facebook Page — stores the page identifier, page name, and the access credential needed to post on its behalf. One per client.
- **VideoUploadJob**: Represents a single upload attempt for an approved video — tracks the video reference, target page, attempt count, and timestamps. Moves through states: `pending → uploading → published / failed`.
- **UploadAttempt**: Represents one individual retry within a VideoUploadJob — records the outcome and any error detail returned by Facebook.

---

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An approved video appears as a live public post on the linked Facebook Page within 2 minutes of the approval event.
- **SC-002**: The owner receives a Telegram confirmation with a post link within 30 seconds of the video going live on Facebook.
- **SC-003**: Upload failures due to transient errors are recovered automatically without owner intervention at least 90% of the time.
- **SC-004**: When an upload cannot be recovered, the owner is alerted via Telegram within 5 minutes of the final failed attempt.
- **SC-005**: The one-time Facebook Page authorization flow can be completed by a non-technical business owner in under 5 minutes.
- **SC-006**: No video is posted to Facebook more than once, even if the approval event is received multiple times.

---

## Clarifications

### Session 2026-05-30

- Q: Where should the Facebook Page access token be stored? → A: Client `.env` file, consistent with existing FieldKit credential pattern.
- Q: How does the administrator generate the Facebook authorization link? → A: A CLI script run manually by the admin that outputs the auth URL, which the admin shares with the owner.
- Q: What states does a VideoUploadJob move through? → A: `pending → uploading → published / failed` (four states).
- Q: What is the retry delay strategy for failed uploads? → A: Fixed 60-second delay between each of the 3 retry attempts.
- Q: What observability is required for upload activity? → A: Log all upload attempts, outcomes, and errors to a per-client log file.

---

## Assumptions

- Each client (e.g., `_demo`) is associated with exactly one Facebook Page for the lifetime of this feature; multi-page support is out of scope.
- The administrator (Sandeep) sets up the Meta Developer App once; clients do not interact with the Meta developer portal.
- Caption text is out of scope; this feature posts video-only. Caption generation is a separate future feature.
- The video file produced by Feature 002 (MP4, 9:16 portrait) meets Facebook's video format requirements without conversion.
- The Facebook authorization flow requires a redirect URL; a minimal hosted endpoint or local tunnel is acceptable for the `_demo` client during development.
- Post scheduling (choosing a future publish time) is out of scope for this feature; all posts go live immediately upon approval.
- The existing Telegram bot infrastructure from Feature 001/002 is reused for all owner notifications.
- Facebook access credentials may expire and will need periodic renewal; alerting the owner is sufficient for this feature — automatic token refresh is out of scope.
