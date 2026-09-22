---
name: process-photos
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
  skill's `name` is automatically a slash command.
- Naming (cross-review finding, issue #18): the agentskills.io spec requires
  `name` to be lowercase letters/digits/hyphens only -- no underscores. This
  file originally used `name: process_photos` to match the admin's existing
  `/process_photos` muscle memory, but that violates the spec. Verified in
  Hermes's own source (`~/.hermes/hermes-agent/agent/skill_commands.py`,
  `scan_skill_commands()`) that this was unnecessary: Hermes *always*
  normalizes a skill's `name` to a hyphenated slug for its internal command
  key (`name.lower().replace('_', '-')`, then strips anything not
  `[a-z0-9-]`), regardless of whether the frontmatter uses `_` or `-`.
  Separately, `hermes_cli/commands.py::_sanitize_telegram_name()` converts
  that hyphenated key back to underscores when registering the Telegram bot
  command, because Telegram itself restricts command names to
  `[a-z0-9_]` -- so the bot command stays `/process_photos` either way.
  `agent/skill_commands.py::resolve_skill_command_key()` also treats `-`/`_`
  as interchangeable on lookup, for the same reason. Confirmed empirically
  from the Hermes venv:
  `scan_skill_commands()` mapped this file to `/process-photos` even before
  this rename (frontmatter still said `process_photos`), and
  `resolve_skill_command_key('process_photos')` already resolved to
  `/process-photos`. So renaming `name:` to `process-photos` here is a
  no-op for dispatch -- verified with `hermes skills list` after the rename
  (see platform/docs/hermes/03-process-photos-skill.md) -- and brings the
  frontmatter into spec compliance. The Telegram-facing command the admin
  actually types stays `/process_photos` (unchanged, per Telegram's own
  underscore-only restriction), which is why the body text below still says
  `/process_photos`, not `/process-photos`.
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

# process-photos

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
# newline followed by a line exactly equal to the delimiter below would still
# end the heredoc early. No bash construct prevents that — each has a finite
# terminator a pathname may contain — so the delimiter is long and improbable
# and the guards below bound the damage. See
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
[ -f "$AGENT_DIR/scripts/process_photos.py" ] || { echo "ERROR: scripts/process_photos.py not found under $AGENT_DIR — this skill is not running from a complete fieldkit checkout, which points at Hermes's skills.external_dirs setting."; exit 1; }
cd "$AGENT_DIR" || { echo "ERROR: cannot enter $AGENT_DIR"; exit 1; }
# Prefer GNU timeout, fall back to macOS's Homebrew-provided gtimeout
# (coreutils), fall back to running with no hard timeout at all if neither
# is installed -- stock macOS ships neither binary (confirmed: a walkthrough
# doc previously claimed this fallback existed when the invocation below was
# actually unconditional `timeout 660 ...`, which fails outright with
# "command not found" on a machine lacking both — see
# platform/docs/hermes/11-manual-e2e-walkthrough.md's pre-flight section).
if command -v timeout >/dev/null 2>&1; then
  TIMEOUT_BIN="timeout 660"
elif command -v gtimeout >/dev/null 2>&1; then
  TIMEOUT_BIN="gtimeout 660"
else
  TIMEOUT_BIN=""
fi
$TIMEOUT_BIN python3 scripts/process_photos.py --project "<extracted_project_name>" 2>&1
```

> **Path resolution (issue #74):** the repo location is derived from this
> skill file's own directory, substituted into the block above by Hermes at
> dispatch time. Moving or renaming the checkout needs no edit here — do NOT
> replace this with an absolute path.

If the block prints a line beginning `ERROR:`, relay that line and stop. Those
conditions are Hermes configuration or checkout-layout problems that the
operator resolves; the agent's role is limited to reporting them, not to
changing configuration, editing files, or restarting the gateway.

Do not access Drive or generate the video yourself.
If neither `timeout` nor `gtimeout` was found (the command above ran with no
wrapper at all), there is no enforced 11-minute hard cap on this machine —
this is a real, expected degraded-but-working mode, not a failure; the
pipeline itself is still bounded by Drive/network timeouts, just not by this
skill's own deadline. `brew install coreutils` (provides `gtimeout`) restores
the hard cap.
If the exit code is 124, report: "⏱️ Video generation timed out — try with fewer photos."
If the exit code is non-zero (and not 124), report it as an error: "Script failed (exit <code>): <output>"
Otherwise relay the output verbatim to the user. Do not summarise or paraphrase.
