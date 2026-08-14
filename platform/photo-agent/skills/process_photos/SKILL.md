---
name: process_photos
description: "Generate a video from photos in a Google Drive project folder and send it to the admin for approval."
version: 1.0.0
author: FieldKit
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [fieldkit, photo-agent, video, telegram]
prerequisites:
  commands: [python3, ffmpeg, gws]
---

<!--
OpenClaw -> Hermes mapping (issue #7, platform/.specify/003-hermes-runtime/spec.md FR-002):

- Frontmatter: OpenClaw's `metadata: {"openclaw": {"requires": {"bins": [...]}}}`
  had no Hermes equivalent (Hermes has no declarative prerequisite-enforcement
  mechanism as of this writing) -- replaced with the agentskills.io-standard
  `prerequisites.commands` field, which is informational only. The actual
  enforcement stays exactly as OpenClaw did it: explicit `which` checks in the
  body below, which is portable and doesn't depend on either runtime's specific
  prerequisite-checking behavior.
- Invocation: OpenClaw needed `user-invocable: true` in frontmatter to expose
  a skill as a manual command. Hermes has no such field -- every installed
  skill's `name` is automatically a slash command (here, `/process_photos`,
  unchanged from today).
- Discovery / sync: OpenClaw skills were manually synced into
  `~/.openclaw/workspace/skills/` (see fieldkit's
  `openclaw_skill_cache` notes -- editing SKILL_*.md required a manual resync
  step or Hermes -- sorry, OpenClaw -- wouldn't see the change). Hermes's
  `skills.external_dirs` config (`~/.hermes/config.yaml`) points directly at
  this file's parent directory inside the fieldkit repo, so there is no copy
  step and no stale-cache risk -- edit this file, Hermes picks it up on the
  next turn. See platform/docs/hermes/03-process-photos-skill.md for the
  exact external_dirs entry.
- Everything else (argument parsing, validation, verbatim relay) is
  unchanged from the OpenClaw skill -- these are LLM-followed prose
  instructions either way, not runtime-specific syntax.
-->

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
cd ~/src/fieldkit/platform/photo-agent || { echo "ERROR: photo-agent directory not found"; exit 1; }
timeout 660 python3 scripts/process_photos.py --project "<extracted_project_name>" 2>&1
```

Do not access Drive or generate the video yourself.
If the exit code is 124, report: "⏱️ Video generation timed out — try with fewer photos."
If the exit code is non-zero (and not 124), report it as an error: "Script failed (exit <code>): <output>"
Otherwise relay the output verbatim to the user. Do not summarise or paraphrase.
