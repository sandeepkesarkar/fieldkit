# 002 — Photo Video Agent: Sequence Diagram

**Last Updated:** 2026-05-12
**Source of truth:** [`techplan.md`](techplan.md) + [`clarify.md`](clarify.md)

> This diagram is generated from the spec. If the spec changes, regenerate using `/speckit.diagram`.

---

## Flow A — `process_photos.py` (command trigger → approval request)

```mermaid
sequenceDiagram
    participant Admin
    participant Telegram as Telegram<br/>(OpenClaw channel)
    participant OpenClaw
    participant Script as process_photos.py
    participant gws as gws CLI
    participant Drive as Google Drive API
    participant FFmpeg
    participant TgAPI as Telegram Bot API<br/>(direct HTTP)
    participant state as state.py
    participant logger as logger.py

    Admin->>Telegram: /process_photos kitchen_remodel
    Telegram->>OpenClaw: route to skill
    OpenClaw->>Script: python3 scripts/process_photos.py --project kitchen_remodel

    note over Script: acquire run.lock (LOCK_EX | LOCK_NB)<br/>exit silently if another instance is running

    Script->>state: get_pending_approval()
    state-->>Script: None

    Script->>logger: log_command("kitchen_remodel")

    note over Script,Drive: PHASE 1 — locate Drive project folder

    Script->>gws: drive files list --params (name="kitchen_remodel", parent=root_folder_id)
    gws->>Drive: GET /files?q=name="kitchen_remodel" AND "root" in parents
    Drive-->>gws: folder metadata
    gws-->>Script: JSON → folder_id

    note over Script,Drive: PHASE 2 — discover and validate photos

    Script->>gws: drive files list --params ("folder_id" in parents)
    gws->>Drive: GET /files?q="folder_id" in parents
    Drive-->>gws: file list with MIME types
    gws-->>Script: JSON → filtered to image/jpeg + image/png, sorted by name

    note over Script: validate: 2 ≤ count ≤ 30<br/>abort with Telegram error if outside range

    note over Script,Drive: PHASE 3 — download photos

    Script->>Script: clear and recreate tmp/kitchen_remodel/

    loop For each photo (alphabetical order)
        Script->>gws: drive files get --fileId {id} --output {local_path}
        gws->>Drive: GET /files/{id}?alt=media
        Drive-->>gws: binary file content
        gws-->>Script: file written to local_path
    end

    Script->>logger: log_downloaded("kitchen_remodel", count=5)

    note over Script: PHASE 4 — scrub (no-op this phase)
    Script->>Script: scrub(photos) → returns photos unchanged

    note over Script,FFmpeg: PHASE 5 — generate video

    Script->>FFmpeg: subprocess(ffmpeg -loop 1 ... -filter_complex "scale,xfade..." -c:v libx264 -an ...)
    FFmpeg-->>Script: output.mp4 written to tmp/kitchen_remodel/

    Script->>logger: log_generated("kitchen_remodel", duration_sec=18, size_bytes=...)

    note over Script,Drive: PHASE 6 — upload video to Drive

    Script->>gws: drive +upload tmp/.../output.mp4 --parent folder_id --name kitchen_remodel_20260512_143200.mp4
    gws->>Drive: POST /files (multipart upload)
    Drive-->>gws: {id: drive_video_file_id}
    gws-->>Script: JSON → drive_video_file_id

    Script->>logger: log_uploaded("kitchen_remodel", drive_video_file_id)

    note over Script,TgAPI: PHASE 7 — send approval request with inline keyboard

    Script->>TgAPI: sendMessage(chat_id, approval_text, reply_markup=[✅ Approve, ❌ Reject])
    TgAPI->>Telegram: deliver message with inline buttons
    Telegram-->>Admin: 📹 Video ready for review — kitchen_remodel<br/>📁 Drive link + ✅/❌ buttons

    Script->>state: set_pending_approval({project_name, folder_id, drive_video_file_id,<br/>drive_folder_link, video_local_path, message_id, triggered_at})
    Script->>logger: log_approval_req("kitchen_remodel", message_id)
    Script->>Script: release run.lock
```

---

## Flow B — `check_approval.py` (cron / on-demand → approve or reject)

```mermaid
sequenceDiagram
    participant Admin
    participant Telegram as Telegram<br/>(OpenClaw channel)
    participant OpenClaw
    participant Cron as System Cron<br/>(1-min interval)
    participant Script as check_approval.py
    participant TgAPI as Telegram Bot API<br/>(direct HTTP)
    participant gws as gws CLI
    participant Drive as Google Drive API
    participant Gmail as Gmail API
    participant state as state.py
    participant logger as logger.py

    alt 1-minute cron fires
        Cron->>Script: python3 scripts/check_approval.py --source cron
    else Admin sends /check_approval
        Admin->>Telegram: /check_approval
        Telegram->>OpenClaw: route to skill
        OpenClaw->>Script: python3 scripts/check_approval.py
    end

    Script->>state: get_pending_approval()
    state-->>Script: pending record (or null)

    alt No pending approval
        note over Script: exit immediately — nothing to do<br/>(silent on cron, brief message on manual)
    else Pending approval found
        Script->>state: get_telegram_offset()
        state-->>Script: offset N

        Script->>TgAPI: GET getUpdates?offset=N&timeout=0
        TgAPI-->>Script: list of updates

        Script->>Script: find callback_query where message_id matches pending record

        alt No matching callback found
            Script->>state: set_telegram_offset(max_update_id + 1)
            note over Script: exit — admin has not responded yet
        else Admin tapped ✅ Approve
            Script->>TgAPI: answerCallbackQuery(callback_query_id)
            TgAPI-->>Admin: spinner dismissed on button

            Script->>gws: gmail users messages send<br/>(approval email to ADMIN_EMAIL)
            gws->>Gmail: POST /users/me/messages/send
            alt Email sent successfully
                Gmail-->>Admin: ✅ Approved — kitchen_remodel<br/>Drive folder link
                Script->>Telegram: openclaw message send "✅ Approved — kitchen_remodel<br/>Email sent to admin@example.com"
                Telegram-->>Admin: Telegram confirmation
            else Email send failed
                Script->>Telegram: openclaw message send<br/>"✅ Approved — but email failed. Drive link: <link>"
                Telegram-->>Admin: fallback Telegram message with link
            end

            Script->>Script: delete local temp video file
            Script->>logger: log_approved("kitchen_remodel")

        else Admin tapped ❌ Reject
            Script->>TgAPI: answerCallbackQuery(callback_query_id)
            TgAPI-->>Admin: spinner dismissed on button

            Script->>gws: drive files delete --fileId drive_video_file_id
            gws->>Drive: DELETE /files/{drive_video_file_id}
            note over gws,Drive: best-effort — failure logged but does not abort rejection

            Script->>Script: delete local temp video file

            Script->>Telegram: openclaw message send<br/>"❌ Rejected — kitchen_remodel<br/>Video removed. Update photos in Drive and run /process_photos kitchen_remodel again."
            Telegram-->>Admin: rejection notification

            Script->>logger: log_rejected("kitchen_remodel")
        end

        Script->>state: clear_pending_approval()
        Script->>state: set_telegram_offset(max_update_id + 1)
    end
```

---

## Error Paths Not Shown Above

| Scenario | Behavior |
|----------|----------|
| Drive folder not found | `DriveFolderNotFoundError` → Telegram error via OpenClaw; script exits |
| Fewer than 2 photos | Telegram error with count; script exits; no download attempted |
| More than 30 photos | Telegram error; script exits |
| Photo download fails | Telegram error with filename; temp dir cleaned; script exits |
| FFmpeg not installed | `FileNotFoundError` on subprocess → Telegram error "FFmpeg not found"; script exits |
| FFmpeg exits non-zero | `VideoGenerationError` with stderr → Telegram error with reason; script exits |
| Drive upload fails | Telegram error; local file retained for manual recovery; script exits |
| `run.lock` held by another instance | Second instance exits 0 silently |
| `pending_approval` exists when `/process_photos` arrives | Telegram error naming the pending project; no download; exits |
| Drive delete fails on rejection | Logged at ERROR; rejection still completes; state still cleared |
| Email fails on approval | Telegram fallback with Drive link; state still cleared |
| Mac Mini reboots while approval pending | `state.json` persists; next `check_approval.py` cron run picks up where it left off |
