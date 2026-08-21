---
name: check-approval
description: "Manually re-check for a pending video approval response from the admin and process it."
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
OpenClaw -> Hermes mapping (issue #8, platform/.specify/003-hermes-runtime/spec.md
FR-002 / FR-002a):

- Frontmatter: same shape as issue #7's process-photos port (SKILL_process_photos.md
  -> skills/process-photos/SKILL.md) -- OpenClaw's `metadata: {"openclaw": ...}`
  block dropped, replaced with the agentskills.io-standard `prerequisites.commands`
  field (informational only; this skill has no binary prerequisites beyond python3,
  unlike process-photos which shells out to ffmpeg/gws).
- Invocation: OpenClaw needed `user-invocable: true` in frontmatter to expose a
  skill as a manual command. Hermes has no such field -- every installed skill's
  `name` is automatically a slash command.
- Naming: `name: check-approval` (hyphenated), not `check_approval`, per the
  agentskills.io spec (lowercase letters/digits/hyphens only -- no underscores).
  Verified empirically against this same skill's install, mirroring #7/#18's
  investigation of `agent/skill_commands.py::scan_skill_commands()`:
  `scan_skill_commands()` normalizes ANY frontmatter `name` to a hyphenated slug
  for its internal command key (`name.lower().replace('_', '-')`, non-`[a-z0-9-]`
  chars stripped) regardless of whether the source used `_` or `-`, and
  `hermes_cli/commands.py::_sanitize_telegram_name()` converts that hyphenated key
  back to underscores when registering the actual Telegram bot command (Telegram
  itself restricts command names to `[a-z0-9_]`). So the Telegram-facing command
  the admin types stays `/check_approval` regardless of the frontmatter spelling
  -- naming this file `check-approval` from the start is a no-op for dispatch and
  brings it into spec compliance immediately, without #18's two-step rename.
  See platform/docs/hermes/04-check-approval-skill.md for the empirical
  verification run against this exact skill (`scan_skill_commands()` /
  `resolve_skill_command_key()` from the Hermes venv).
- **Button-callback dispatch is NOT ported to Hermes (FR-002a, amended 2026-08-21).**
  OpenClaw's SKILL_check_approval.md had three trigger sections: "Approve button
  (callback_data == approve)", "Reject button (callback_data == reject)", and
  "invoked manually (/check_approval command)". Only the third is reachable
  through Hermes. Verified empirically (not assumed by analogy to OpenClaw, and
  not assumed by analogy to #7's naming question, which WAS a configuration
  question -- this one is not): Hermes's Telegram adapter
  (`plugins/platforms/telegram/adapter.py::_handle_callback_query`) only
  recognizes a closed set of Hermes-internal `callback_data` prefixes (`mp:`,
  `cp:`, `gt:`, `ea:`, `sc:`, `cl:`, `update_prompt:`). FieldKit's own approval
  buttons (`tools/telegram_api.py::send_message_with_buttons`) send bare
  `callback_data` of `"approve"` / `"reject"` -- neither matches any recognized
  prefix, so the handler falls through every branch and returns having done
  nothing: no `answer_callback_query`, no `edit_message_text`, and critically no
  skill or agent-turn dispatch of any kind. Confirmed by invoking
  `_handle_callback_query` directly under Hermes's own venv with both values (see
  platform/docs/hermes/04-check-approval-skill.md for the exact probe and
  output). `_normalize_platform_event`, Hermes's only other generic inbound-event
  hook, is wired for `message_reaction` / `edited_message` only and returns
  `None` for a `callback_query` update -- no alternate escape hatch. Hermes's own
  `docs/relay-connector-contract.md` documents the identical posture by design:
  "Foreign callback payloads (another integration's buttons) never become prompt
  events... dropped at the connector." Button taps continue to be handled
  exclusively by `check_approval.py`'s pre-existing cron leg (unchanged, FR-003)
  -- this skill covers ONLY the manual `/check_approval` command trigger.
- Discovery / sync: same as process-photos -- Hermes's `skills.external_dirs`
  config points at this file's parent directory inside the fieldkit repo
  directly, no copy step.
- Everything else (script invocation, verbatim relay) is unchanged from the
  OpenClaw skill's manual-command section.
-->

# check-approval

## When invoked manually (e.g. `/check_approval` command)

The admin types `/check_approval` in Telegram, typically after tapping an
Approve button whose automatic processing (the cron leg — see the mapping
comment above) hasn't caught up yet, to force an immediate re-check.

Do not ask the user any clarifying questions — always run this block
immediately:

```bash
cd ~/src/fieldkit/platform/photo-agent || { echo "ERROR: photo-agent directory not found"; exit 1; }
python3 scripts/check_approval.py --callback-data approve 2>&1
```

<!--
Callback-data -> script-argument mapping (issue #8):

check_approval.py's `--callback-data` flag accepts exactly two values,
`approve` or `reject` (see scripts/check_approval.py's argparse `choices`).
This skill only ever passes `approve`:

- There is no manual `/check_approval_reject` (or equivalent) command, under
  OpenClaw or Hermes — a reject decision has always been expressed by tapping
  the Reject button in Telegram, never by typing a command. The manual
  `/check_approval` command exists purely as a "re-check now" nudge for the
  admin after having already tapped Approve, in case the automatic path is
  slow — so `approve` is the only value that can ever correctly correspond to
  a manually-typed invocation.
- Both the Approve *and* Reject button taps (`callback_data == "approve"` /
  `callback_data == "reject"`) are handled entirely by check_approval.py's own
  cron leg (`--source cron`, no `--callback-data` flag at all — it reads the
  decision from Telegram's `getUpdates` response body, not from a CLI arg).
  That cron invocation is unchanged by this skill and is not something this
  skill (or Hermes) invokes — see the mapping comment above for why Hermes
  cannot dispatch on the raw button tap at all.
- If `check_approval.py`'s state has no pending approval whatsoever (e.g. the
  admin fat-fingered `/check_approval` with nothing outstanding, or the cron
  leg already resolved it), the script exits 0 with no output — see "Output
  handling" below.
-->

## Output handling

Do not interpret the output yourself. Run the script once and do not retry.
If the exit code is non-zero, report it as an error: "Script failed (exit <code>): <output>"
If the script exits with code 0 and no output, report: "No pending approval."
Otherwise relay the output verbatim. Do not summarise or paraphrase.
