---
name: process_photos
description: Generate a video from photos in a Google Drive project folder and send it to the admin for approval
user-invocable: true
metadata: {"openclaw": {"requires": {"bins": ["gws", "python3", "ffmpeg"]}}}
---

# process_photos

The admin provides a project name after the command (e.g. `/process_photos kitchen_remodel`).

Extract the project name — everything after `/process_photos` (strip any `@botname` suffix from the command first, e.g. `/process_photos@mybot` → `/process_photos`), trimmed of all leading and trailing whitespace.

If the trimmed result is empty, reply:
"Please provide a project name — e.g. /process_photos kitchen_remodel"

Validate the extracted project name: it must match the pattern `^[A-Za-z0-9_-]+$` (letters, numbers, underscores, and hyphens only — no spaces or special characters).
If it does not match, reply:
"Invalid project name. Use only letters, numbers, underscores, and hyphens — e.g. /process_photos kitchen_remodel"

Otherwise verify the required tools are on PATH:

```bash
which ffmpeg || { echo "ERROR: ffmpeg not found — run: brew install ffmpeg"; exit 1; }
which gws || { echo "ERROR: gws not found — check installation"; exit 1; }
```

If either check fails, report the error and stop. Otherwise run:

```bash
cd ~/src/fieldkit/clients/_demo/src/photo-agent || { echo "ERROR: photo-agent directory not found"; exit 1; }
timeout 300 python3 scripts/process_photos.py --project "<extracted_project_name>" 2>&1
```

Do not access Drive or generate the video yourself.
If the exit code is non-zero (including 124 for timeout), report it as an error: "Script failed (exit <code>): <output>"
Otherwise relay the output verbatim to the user. Do not summarise or paraphrase.
