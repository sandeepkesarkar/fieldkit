---
name: photo-approve
description: "Approve the pending video: send the approval email, enqueue the Facebook upload if configured, and notify the admin."
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
Text-based approval (issue #49, superseding issue #8's manual /check_approval
command and platform/.specify/003-hermes-runtime/spec.md FR-002/FR-002a):

- This skill replaces `check-approval` (renamed here). Before #49, this
  file's `/check_approval` command was a "re-check now" nudge for an admin
  who had already tapped an inline Approve button, because Hermes has no
  hook for a raw button-tap `callback_query` at all (verified empirically —
  see platform/docs/hermes/04-check-approval-skill.md and
  07-callback-race-fix.md). #49 removes the buttons entirely: this skill is
  now the ONLY way an approval happens, not a fallback for a slower cron
  poller. See platform/docs/hermes/10-text-based-approval-migration.md for
  the full writeup, including fresh empirical dispatch verification for the
  renamed command key.
- **Naming — NOT `approve` (architectural fork, resolved with the repo
  owner via AskUserQuestion rather than guessed):** Hermes reserves a
  built-in core command named `approve` (`CommandDef("approve", "Approve a
  pending dangerous command", ...)` in `hermes_cli/commands.py` — Hermes's
  own dangerous-shell-command approval gate, unrelated to FieldKit).
  Verified empirically against this machine's installed Hermes: a skill
  literally named `approve` triggers `scan_skill_commands()`'s own
  core-command-collision guard (`agent/skill_commands.py`, the
  `resolve_command(cmd_name) is not None` check) and is skipped from slash
  auto-registration entirely — only reachable via the verbose `/skill
  approve`, not a plain `/approve`. No frontmatter field lets a skill claim
  a slash command different from its normalized `name`, and patching
  Hermes's own core command registry was rejected as out of scope, same
  posture issue #29 already took on the button-callback question. Given
  three options put to the repo owner (rename only the colliding command;
  rename both for a symmetric pair; something else), the owner chose the
  symmetric pair: `photo-approve` / `photo-reject`. (`reject` alone did NOT
  collide with any core command — the rename to `photo-reject` is for
  naming symmetry with this skill, not a second collision.)
- Naming mechanics: `name: photo-approve` is already agentskills.io-compliant
  (lowercase letters/digits/hyphens). Hermes's `scan_skill_commands()`
  normalizes this to the command key `/photo-approve`; per the hyphen/
  underscore rule #8 documented for `check-approval`, the Telegram-facing
  command the admin actually types is `/photo_approve` (Telegram restricts
  bot command names to `[a-z0-9_]`, so `hermes_cli/commands.py::_sanitize_telegram_name()`
  converts the hyphen). Verified for this exact rename in
  platform/docs/hermes/10-text-based-approval-migration.md.
- Invocation: unchanged from the pre-#49 manual-command path — shell out to
  check_approval.py with `--callback-data approve`. What changed is what
  that flag means: it used to be one of two ways an approval could happen
  (the other being a real button tap, handled by a since-removed cron
  poller); it is now the only way. check_approval.py's own module docstring
  and platform/docs/hermes/10-text-based-approval-migration.md have the full
  before/after.
- Discovery / sync: unchanged — Hermes's `skills.external_dirs` config
  points at this file's parent directory inside the fieldkit repo directly,
  no copy step.
-->

# photo-approve

The admin types `/photo_approve` in Telegram to approve the pending video.

The command is fully specified by the single pending record in state.json. On
invocation, use the following script as the sole execution path, exactly once.
The script owns state access, validation, and all approval side effects
(sending the email, enqueueing the Facebook upload if configured, activity
logging); the agent's role is limited to invoking it and reporting the result.

```bash
# Repo location is derived from this skill file's own directory. Hermes
# substitutes HERMES_SKILL_DIR into this block TEXTUALLY, before Bash parses
# it, so the substituted path is shell SOURCE, not shell data: inside double
# quotes a path containing $, $(...) or a backtick would still expand or
# execute, and inside single quotes a path containing a single quote would
# break out of the quoting. A quoted heredoc (<<'DELIM') expands nothing at
# all, so the path arrives as literal data whatever it contains. Never
# hardcode an absolute repo path here either — a moved checkout silently
# broke every command for three weeks that way (issue #74).
# Residual, documented rather than papered over: a pathname containing a
# newline followed by a line exactly equal to the delimiter below ends the
# heredoc early, and the rest of the pathname is then arbitrary shell source
# that can bypass every guard here, print no ERROR and exit 0. No bash
# construct prevents that — each has a finite terminator a pathname may
# contain. The delimiter is long and improbable to rule out coincidence, not
# an adversary; the risk is accepted because creating such a path needs write
# access to Hermes's config, i.e. code execution as this user already. See
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
python3 scripts/check_approval.py --callback-data approve 2>&1
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
> pending" — that is the ONLY case with no output. A successful approval
> always prints a one-line confirmation (e.g. `Approved: <project>`) before
> exiting 0, so never report "No pending approval" when stdout is non-empty,
> even though the exit code is 0 in both cases — check the output, not just
> the exit code. Lock contention (another decision already being processed)
> also exits 0 with non-empty output (e.g. `Already processing — try again
> in a moment.`) — it falls under "otherwise relay the output verbatim"
> above like any other non-empty-output case, not under "No pending
> approval".
