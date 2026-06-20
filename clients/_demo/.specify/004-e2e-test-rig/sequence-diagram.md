# Feature 004 — Sequence Diagram

**Feature:** End-to-End Test Rig (Happy Path)
**Generated:** 2026-06-20

```mermaid
sequenceDiagram
    actor Admin
    participant Rig as run_e2e_test.py
    participant FFmpeg
    participant Drive as Google Drive
    participant Photos as process_photos.py
    participant StateJSON as state.json
    participant Telegram
    participant CheckApproval as check_approval.py (cron)
    participant FBState as facebook_state.json
    participant Upload as upload_facebook.py (cron)
    participant Facebook as Facebook Graph API

    Admin->>Rig: python3 scripts/run_e2e_test.py --duration 30

    Note over Rig: Pre-flight: verify env vars, no pending approval, FFmpeg on PATH

    Note over Rig,FFmpeg: Stage 1 — Clock frame generation
    Rig->>FFmpeg: generate 9 × frame_NNN.jpg (rate=1, localtime PTS, MM/DD/YYYY HH:MM:SS)
    FFmpeg-->>Rig: frame_001.jpg … frame_009.jpg (each shows start_unix + N seconds)
    Rig-->>Admin: [14:05:01] Stage 1/5: Clock frames generated (9 frames, ~32s video) ✅ (2s)

    Note over Rig,Drive: Stage 2 — Drive upload
    Rig->>Drive: create_folder("e2e-test-YYYYMMDD-HHMMSS")
    Drive-->>Rig: folder_id
    Rig->>Drive: upload frame_001.jpg … frame_009.jpg (content_type=image/jpeg)
    Drive-->>Rig: drive_file_ids[0…8]
    Rig-->>Admin: [14:05:03] Stage 2/5: Drive upload (9 files) ✅ (14s)

    Note over Rig,StateJSON: Stage 3 — process_photos.py + Telegram approval
    Rig->>Photos: subprocess --project e2e-test-YYYYMMDD-HHMMSS (SECONDS_PER_PHOTO=4)
    Photos->>Drive: find_folder + list_photos + download frames
    Drive-->>Photos: 9 JPEG files
    Photos->>FFmpeg: generate slideshow video (~32s, clock advancing each slide)
    FFmpeg-->>Photos: video.mp4
    Photos->>Drive: upload video.mp4
    Drive-->>Photos: drive_video_file_id
    Photos->>Telegram: send_message_with_buttons("Approve / Reject")
    Telegram-->>Photos: message_id
    Photos->>StateJSON: set_pending_approval(project_name, message_id, video_local_path, ...)
    Photos-->>Rig: subprocess exits 0

    Rig->>StateJSON: confirm pending_approval.project_name == test_name
    Rig-->>Admin: [14:05:17] Stage 3/5: process_photos.py + Telegram sent ✅ (42s)
    Rig-->>Admin: Tap Approve in Telegram to continue (timeout: 10m)

    Admin->>Telegram: tap Approve button

    Note over CheckApproval,FBState: check_approval.py cron fires (≤1 min after tap)
    CheckApproval->>StateJSON: get_pending_approval()
    StateJSON-->>CheckApproval: record
    CheckApproval->>Telegram: answer_callback_query + remove buttons
    CheckApproval->>FBState: set_pending_upload(project_name, video_local_path, ...)
    CheckApproval->>StateJSON: clear_pending_approval()

    Note over Rig,FBState: Stage 4 — Approval received (polling)
    Rig->>StateJSON: poll until pending_approval == None
    Rig->>FBState: poll until pending_upload.project_name == test_name
    StateJSON-->>Rig: cleared
    FBState-->>Rig: found
    Rig-->>Admin: [14:06:55] Stage 4/5: Approval received ✅ (56s)

    Note over Upload,Facebook: upload_facebook.py cron fires (≤1 min after stage 4)
    Upload->>FBState: get_pending_upload()
    FBState-->>Upload: record (video_local_path still on disk — not deleted by check_approval.py)
    Upload->>Facebook: POST /{page_id}/videos (video_local_path)
    Facebook-->>Upload: post_id
    Upload->>FBState: mark_published(post_id)
    Upload->>Upload: delete local video file
    Upload->>Telegram: send confirmation

    Note over Rig,FBState: Stage 5 — Facebook post live (polling)
    Rig->>FBState: poll until pending_upload.status == "published"
    FBState-->>Rig: published
    Rig-->>Admin: [14:07:50] Stage 5/5: Facebook post live ✅ (55s)
    Rig-->>Admin: ✅ All stages passed. Total: 2m 50s
    Rig-->>Admin: Post: https://www.facebook.com/{post_id}
```

> Note: `_delete_local_file` is moved from `check_approval.py` to `upload_facebook.py`'s success path (research.md §4). The local video file must survive until `upload_facebook.py` has used it.
