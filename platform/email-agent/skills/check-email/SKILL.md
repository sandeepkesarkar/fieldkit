---
name: check-email
description: "Check Gmail inbox for new emails and send Telegram acknowledgements for each valid email received."
version: 1.0.0
author: FieldKit
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [fieldkit, email-agent, gmail, telegram]
prerequisites:
  commands: [python3, gws]
---

<!--
OpenClaw -> Hermes mapping (issue #25, platform/.specify/003-hermes-runtime/spec.md FR-002):

- Frontmatter: OpenClaw's `metadata: {"openclaw": {"requires": {"bins": [...]}}}`
  had no Hermes equivalent, same finding as #7/#8 -- replaced with the
  agentskills.io-standard `prerequisites.commands` field (informational
  only). This skill has no explicit `which` checks in its body (unlike
  process-photos, which checks ffmpeg/gws before running) because
  check_email.py itself already fails loudly and reports a specific error
  if gws or its auth is missing -- there is nothing extra for the skill body
  to validate before dispatch, mirroring check-approval's equally thin body.
- Invocation: OpenClaw needed `user-invocable: true` in frontmatter to expose
  a skill as a manual command. Hermes has no such field -- every installed
  skill's `name` is automatically a slash command.
- Naming (verified empirically against Hermes's own source, not assumed by
  analogy to #7/#8 -- same evidentiary bar #18 set): confirmed directly in
  this machine's Hermes install (`~/.hermes/hermes-agent`) that
  `agent/skill_commands.py::scan_skill_commands()` normalizes any
  frontmatter `name` to a hyphenated slug for its internal command key
  (`name.lower().replace('_', '-')`, then strips anything outside
  `[a-z0-9-]`), and `hermes_cli/commands.py::_sanitize_telegram_name()`
  converts that hyphenated key back to underscores when registering the
  actual Telegram bot command, because Telegram itself restricts command
  names to `[a-z0-9_]`. Named this skill `check-email` (hyphenated,
  agentskills.io-compliant) from the start -- no two-step rename needed, the
  same posture #8 took for check-approval. The Telegram-facing command the
  admin types stays `/check_email` (unchanged, per Telegram's own
  underscore-only restriction). See
  platform/docs/hermes/08-check-email-skill.md for the exact probe run
  against this skill's actual files and its output.
- No arguments, no button-callback trigger: unlike process-photos (which
  takes a project-name argument) or check-approval (which has a
  Hermes-unreachable button-callback trigger alongside its manual command),
  check_email.py takes no arguments for a manual run (`--source` defaults to
  `"user"`, the correct default for this trigger) and has no button/callback
  surface at all -- Telegram acknowledgements are outbound-only
  notifications, not interactive messages with callback_data. So this skill
  has exactly one trigger and no argument-extraction step, the simplest of
  the three ported skills.
- Discovery / sync: same as process-photos/check-approval -- Hermes's
  `skills.external_dirs` config (`~/.hermes/config.yaml`) should point at
  this file's parent directory (`platform/email-agent/skills`) inside the
  fieldkit repo, so there is no copy step and no stale-cache risk once
  configured. See SETUP.md's skill-install step for the exact config entry
  and the one-time `external_dirs` addition this skill requires (it lives in
  a different parent directory than the photo-agent skills already
  registered there).
- Everything else (script invocation, verbatim relay) is unchanged from the
  OpenClaw skill's body.
-->

# check-email

The admin types `/check_email` in Telegram to manually trigger an email
intake cycle. Do not ask the user any clarifying questions — always run this
block immediately:

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
IFS= read -r SKILL_DIR <<'FIELDKIT_SKILL_DIR_EOF'
${HERMES_SKILL_DIR}
FIELDKIT_SKILL_DIR_EOF
# An unsubstituted placeholder arrives as the literal token (the heredoc does
# not expand it), so test for that text rather than for an empty value.
case "$SKILL_DIR" in
  ""|*HERMES_SKILL_DIR*) echo "ERROR: FieldKit skill directory unresolved — the HERMES_SKILL_DIR placeholder reached the shell unsubstituted, which points at Hermes's skills.template_vars setting."; exit 1 ;;
esac
# Defence in depth behind the heredoc: a checkout path carrying a shell
# metacharacter is pathological, and refusing it loudly is safer than
# interpolating it correctly here and having it re-parsed somewhere else.
case "$SKILL_DIR" in
  *'$'*|*'`'*|*'"'*|*"'"*|*'\'*) echo "ERROR: FieldKit refusing to run — the skill directory contains a shell metacharacter (a dollar sign, backtick, quote or backslash) and cannot be used safely: $SKILL_DIR"; exit 1 ;;
esac
AGENT_DIR="$(cd "$SKILL_DIR/../.." 2>/dev/null && pwd)" || { echo "ERROR: cannot resolve the email-agent directory two levels above the skill directory: $SKILL_DIR"; exit 1; }
# The two-levels-up step assumes the fieldkit layout <repo>/platform/<agent>/
# skills/<skill>. Hermes also supports skills kept in its own skills
# directory, where two levels up is Hermes's profile rather than an agent
# directory; this refuses that case instead of relying on the script check
# below to notice.
case "$AGENT_DIR" in
  */platform/*) ;;
  *) echo "ERROR: resolved directory is not a fieldkit platform agent directory: $AGENT_DIR"; exit 1 ;;
esac
[ -f "$AGENT_DIR/scripts/check_email.py" ] || { echo "ERROR: scripts/check_email.py not found under $AGENT_DIR — this skill is not running from a complete fieldkit checkout, which points at Hermes's skills.external_dirs setting."; exit 1; }
cd "$AGENT_DIR" || { echo "ERROR: cannot enter $AGENT_DIR"; exit 1; }
python3 scripts/check_email.py
```

> **Path resolution (issue #74):** the repo location is derived from this
> skill file's own directory, substituted into the block above by Hermes at
> dispatch time. Moving or renaming the checkout needs no edit here — do NOT
> replace this with an absolute path.

If the block prints a line beginning `ERROR:`, relay that line and stop. Those
conditions are Hermes configuration or checkout-layout problems that the
operator resolves; the agent's role is limited to reporting them, not to
changing configuration, editing files, or restarting the gateway.

Do not improvise or read emails yourself. The script handles everything: Gmail
polling, allowlist enforcement, Telegram acknowledgements, stale alerts, and
cycle logging.

If the exit code is non-zero, report it as an error: "Script failed (exit
<code>): <output>"
Otherwise relay the output verbatim to the user. Do not summarise or
paraphrase.
