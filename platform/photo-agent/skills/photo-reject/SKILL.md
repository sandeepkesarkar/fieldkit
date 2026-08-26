---
name: photo-reject
description: "Reject the pending video: delete it from Drive and the local temp directory, and notify the admin."
version: 1.0.0
author: FieldKit
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [fieldkit, photo-agent, telegram, approval]
prerequisites:
  commands: [python3]
---

<!--
Text-based approval (issue #49): new skill, sibling to `photo-approve`.
Before #49, a reject decision could only be expressed by tapping the Reject
inline button — there was no manual `/check_approval_reject` (or
equivalent) command, because check_approval.py's cron-based poller was the
only thing that ever passed `--callback-data reject` to the script. See
platform/photo-agent/skills/photo-approve/SKILL.md (renamed from
check-approval) and platform/docs/hermes/10-text-based-approval-migration.md
for the full before/after.

Naming — `photo-reject`, not the shorter `reject`: `reject` alone does NOT
collide with any Hermes core command (verified empirically, same probe as
photo-approve/SKILL.md's naming note) — only `approve` does. This skill is
still named with the `photo-` prefix for symmetry with its sibling, per the
repo owner's explicit choice when presented with the `approve` collision
(rename only the colliding command vs. a symmetric pair vs. something
else) — not because `reject` itself needed renaming.

Naming mechanics: `name: photo-reject` is agentskills.io-compliant.
Hermes's `scan_skill_commands()` normalizes it to the command key
`/photo-reject`; the Telegram-facing command the admin types is
`/photo_reject` (hyphen -> underscore, Telegram's own restriction — see
photo-approve/SKILL.md's naming-mechanics note for the exact mechanism).

check_approval.py's `--callback-data` flag has accepted `reject` since
issue #8 (the script's shared approve/reject business logic has always
handled both outcomes); this skill is what newly exposes that existing
`reject` branch as a command an admin can actually type.
-->

# photo-reject

The admin types `/photo_reject` in Telegram to reject the pending video.

The command is fully specified by the single pending record in state.json. On
invocation, use the following script as the sole execution path, exactly once.
The script owns state access, validation, and all rejection side effects
(deleting the video from Drive, deleting the local temp file, activity
logging); the agent's role is limited to invoking it and reporting the result.

```bash
cd ~/src/fieldkit/platform/photo-agent || { echo "ERROR: photo-agent directory not found"; exit 1; }
python3 scripts/check_approval.py --callback-data reject 2>&1
```

> **Note:** `~/src/fieldkit/` is the expected repo location. If the repo is cloned
> elsewhere, update this path before saving.

## Output handling

Run the script once and do not retry. Report the result as follows:

If the exit code is non-zero, report it as an error: "Script failed (exit <code>): <output>"
If the script exits with code 0 and no output, report: "No pending approval."
Otherwise relay the output verbatim. Do not summarise or paraphrase.
