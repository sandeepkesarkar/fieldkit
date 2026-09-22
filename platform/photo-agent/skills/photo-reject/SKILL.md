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
# Repo location is derived from this skill file's own directory. Hermes
# substitutes HERMES_SKILL_DIR into this block TEXTUALLY, before Bash parses
# it, so the substituted path is shell SOURCE, not shell data: inside double
# quotes a path containing $, $(...) or a backtick would still expand or
# execute, and inside single quotes a path containing a single quote would
# break out of the quoting. A quoted heredoc (<<'DELIM') expands nothing at
# all, which removes those classes — but NOT the delimiter collision described
# next. Never hardcode an absolute repo path here either — a moved checkout
# silently broke every command for three weeks that way (issue #74).
# Residual, documented rather than papered over: a pathname containing a
# newline followed by a line exactly equal to the delimiter below ends the
# heredoc early, and the rest of the pathname is then arbitrary shell source —
# it can bypass the guards here, print no ERROR and exit 0. This is inherent
# to pasting text into shell source, not a gap in these guards. The long
# delimiter rules out coincidence, not an adversary; the risk is accepted
# because creating such a path needs write access to Hermes's config, i.e.
# code execution as this user already. See
# platform/docs/hermes/12-skill-path-resolution.md.
IFS= read -r SKILL_DIR <<'__FIELDKIT_SKILL_DIR_EOF_9c1f4b7e2a5d__'
${HERMES_SKILL_DIR}
__FIELDKIT_SKILL_DIR_EOF_9c1f4b7e2a5d__
[ -n "${SKILL_DIR//[[:space:]]/}" ] || { echo "ERROR: FieldKit skill directory unresolved — the substituted value is empty or blank."; exit 1; }
# An unsubstituted placeholder arrives as the literal token, because the
# quoted heredoc does not expand it either. The comparison value is assembled
# from fragments so Hermes's own substitution regex does not rewrite this line
# along with the placeholder above; matching the literal keeps the diagnostic
# exact instead of misreporting a real path that merely contains the name.
FIELDKIT_PLACEHOLDER='${'"HERMES_SKILL_DIR"'}'
case "$SKILL_DIR" in
  "$FIELDKIT_PLACEHOLDER") echo "ERROR: FieldKit skill directory unresolved — the HERMES_SKILL_DIR placeholder reached the shell unsubstituted, which points at Hermes's skills.template_vars setting."; exit 1 ;;
esac
# Defence in depth behind the heredoc: a checkout path carrying a shell
# metacharacter is pathological, and refusing it loudly is safer than
# interpolating it correctly here and having it re-parsed somewhere else.
case "$SKILL_DIR" in
  *'$'*|*'`'*|*'"'*|*"'"*|*'\'*) echo "ERROR: FieldKit refusing to run — the skill directory contains a shell metacharacter (a dollar sign, backtick, quote or backslash) and cannot be used safely: $SKILL_DIR"; exit 1 ;;
esac
AGENT_DIR="$(cd "$SKILL_DIR/../.." 2>/dev/null && pwd)" || { echo "ERROR: cannot resolve the photo-agent directory two levels above the skill directory: $SKILL_DIR"; exit 1; }
# The two-levels-up step assumes the fieldkit layout <repo>/platform/<agent>/
# skills/<skill>. Hermes also supports skills kept in its own skills
# directory, where two levels up is Hermes's profile rather than an agent
# directory; this refuses that case instead of relying on the script check
# below to notice.
case "$AGENT_DIR" in
  */platform/*) ;;
  *) echo "ERROR: resolved directory is not a fieldkit platform agent directory: $AGENT_DIR"; exit 1 ;;
esac
[ -f "$AGENT_DIR/scripts/check_approval.py" ] || { echo "ERROR: scripts/check_approval.py not found under $AGENT_DIR — this skill is not running from a complete fieldkit checkout, which points at Hermes's skills.external_dirs setting."; exit 1; }
cd "$AGENT_DIR" || { echo "ERROR: cannot enter $AGENT_DIR"; exit 1; }
python3 scripts/check_approval.py --callback-data reject 2>&1
```

> **Path resolution (issue #74):** the repo location is derived from this
> skill file's own directory, substituted into the block above by Hermes at
> dispatch time. Moving or renaming the checkout needs no edit here — do NOT
> replace this with an absolute path.

If the block prints a line beginning `ERROR:`, relay that line and stop. Those
conditions are Hermes configuration or checkout-layout problems that the
operator resolves; the agent's role is limited to reporting them, not to
changing configuration, editing files, or restarting the gateway.

## Output handling

Run the script once and do not retry. Report the result as follows:

If the exit code is non-zero, report it as an error: "Script failed (exit <code>): <output>"
If the script exits with code 0 and no output, report: "No pending approval."
Otherwise relay the output verbatim. Do not summarise or paraphrase.

> **Contract (issue #63):** exit 0 with EMPTY stdout means "nothing was
> pending" — that is the ONLY case with no output. A successful rejection
> always prints a one-line confirmation (e.g. `Rejected: <project>`) before
> exiting 0, so never report "No pending approval" when stdout is non-empty,
> even though the exit code is 0 in both cases — check the output, not just
> the exit code. Lock contention (another decision already being processed)
> also exits 0 with non-empty output (e.g. `Already processing — try again
> in a moment.`) — it falls under "otherwise relay the output verbatim"
> above like any other non-empty-output case, not under "No pending
> approval".
