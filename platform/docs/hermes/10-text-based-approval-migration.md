# Text-Based Approval — Design, Verification, and Live-Migration Guide (issue #49)

Closes issue #49: eliminates the separate Telegram-polling cron job for
photo approvals entirely, replacing the inline Approve/Reject buttons +
`callback_query` flow with plain `/photo_approve` and `/photo_reject`
Hermes commands, dispatched through Hermes's own always-running gateway
poller — no new polling process of any kind.

Source: [`platform/.specify/003-hermes-runtime/spec.md`](../../.specify/003-hermes-runtime/spec.md)
(FR-002/FR-002a/FR-003). Builds on
[`04-check-approval-skill.md`](04-check-approval-skill.md) (#8) and
[`07-callback-race-fix.md`](07-callback-race-fix.md) (#29), both marked
superseded by this doc — see "What this supersedes" below.

## Background — why the poller is gone, not just faster

- Issue #31 (cron-cadence vs. Telegram callback-query freshness race) was
  mitigated but not eliminated by PR #42's fast-ack redesign — a residual
  ~15s window remained, reproduced live twice on 2026-08-26.
- Investigation confirmed: plain text/slash-command messages have **no**
  callback-freshness deadline in Telegram's API — only `callback_query`
  (button taps) do. Routing approval through Hermes's gateway (already
  continuously polling `TELEGRAM_BOT_TOKEN`) eliminates the race
  architecturally rather than shrinking it further.
- The existing `check-approval` Hermes skill (issue #8 / PR #21) already
  performed a *real* approval (not just a status check) by invoking
  `check_approval.py --callback-data approve` directly, bypassing
  `getUpdates` entirely — Hermes has no hook for a raw button-tap
  `callback_query` at all (`04-check-approval-skill.md`). Rejection
  (`--callback-data reject`) already existed in the script but was never
  exposed as a Hermes command.
- The system already operated on a single-pending-approval-at-a-time model
  (`state.json`'s `pending_approval` field is singular) — the existing
  buttons' `callback_data` was already just the bare string
  `approve`/`reject`, no project identifier. So a bare `/photo_approve` or
  `/photo_reject` text command carries **identical semantics** to the
  existing buttons — no new safety concern is introduced by dropping them.

## What changed

1. **`/photo_approve` and `/photo_reject` are now the only way an approval
   happens.** `check-approval`'s manual-command path (which already shelled
   out to `check_approval.py --callback-data approve`) is renamed and
   promoted from a "re-check now" nudge to the sole dispatch path. A new
   sibling skill exposes `--callback-data reject` the same way. Both invoke
   `check_approval.py`'s existing shared approve/reject business logic
   directly — email, Facebook pending-upload state + idempotency, Drive
   cleanup on reject, activity logging, file locking — with zero
   duplication.
2. **The inline Approve/Reject buttons are gone.** `process_photos.py`'s
   approval-request message is now plain text: "Reply /photo_approve or
   /photo_reject." — see `_approval_text()`.
3. **`check_approval.py`'s cron-based polling loop is retired entirely.**
   The `--source cron` flag, `getUpdates` polling, offset tracking
   (`state.get_telegram_offset`/`set_telegram_offset`, now removed from
   `tools/state.py`), callback matching (`_find_matching_callback`), and
   button-tap acknowledgement/removal (`_acknowledge_tap`, `_remove_buttons`,
   `_tap_toast`) are all deleted. `check_approval.py`'s `_run()` is now a
   thin, argument-driven function — parse `--callback-data
   {approve,reject}`, act, done — invoked synchronously per Hermes command,
   never as a standalone background process.
4. **One Telegram bot per client, not two.** `TELEGRAM_APPROVAL_BOT_TOKEN`
   is retired along with the button/poller flow it existed to protect from
   a `getUpdates` offset race (issue #29) — with no second poller left to
   race against Hermes's own continuous long-poll, the whole reason for a
   second bot is gone. `tools/telegram_api.py` is simplified to match
   `email-agent`'s existing single-bot pattern: `send_message()` is the
   module's only function, always on `TELEGRAM_BOT_TOKEN`, and now returns
   the Telegram `message_id` (needed for `state.json`'s
   `telegram_message_id`, which is unrelated to the removed polling and
   stays — it's also the Facebook-upload idempotency key).

## Naming — `/photo_approve` / `/photo_reject`, not the issue's original `/approve` / `/reject`

A real architectural fork, found empirically and put to the repo owner via
`AskUserQuestion` rather than guessed at, same posture issue #29's own
alternatives-evaluation took:

Hermes reserves a **built-in core command** named `approve` — its own
dangerous-shell-command approval gate (`hermes_cli/commands.py`), unrelated
to FieldKit:

```
CommandDef("approve", "Approve a pending dangerous command", "Session",
           gateway_only=True, args_hint="[session|always]", busy_policy="dispatch")
```

`agent/skill_commands.py::scan_skill_commands()` has an explicit
core-command-collision guard — a skill whose normalized command name
matches any core command (via `hermes_cli.commands.resolve_command()`,
which also covers aliases) is silently skipped from slash
auto-registration, reachable only via the verbose `/skill approve`:

```python
if resolve_command(cmd_name) is not None:
    logger.warning(
        "Skill %r generates slash command '/%s' which "
        "collides with a core Hermes command; skipping "
        "auto-registration. Use '/skill %s' instead.",
        name, cmd_name, name,
    )
    continue
```

Verified directly against this machine's installed Hermes
(`~/.hermes/hermes-agent`, v0.20.5) with a skill literally named `approve`:
zero `/approve` entries in `scan_skill_commands()`'s output, and the exact
warning above logged. No frontmatter field lets a skill claim a slash
command different from its normalized `name` — patching Hermes's own core
command registry to remove the collision was rejected as out of scope, same
posture issue #29 already took on the button-callback dispatch question.

`reject` alone does **not** collide with any core command or alias
(confirmed the same way — `resolve_command("reject")` returns `None`).

Given the options — rename only the colliding command (`/approve-video` +
`/reject`), rename both for a symmetric pair, or something else — the repo
owner chose the **symmetric pair**: `photo-approve` / `photo-reject`.
Telegram itself restricts bot command names to `[a-z0-9_]`
(`hermes_cli/commands.py::_sanitize_telegram_name()` converts hyphens to
underscores when registering the actual bot command, same mechanism
`check-approval`'s original naming note documented), so the command the
admin actually types in Telegram is `/photo_approve` / `/photo_reject`
(underscored), while the skill files and Hermes's internal command key use
the hyphenated form (`photo-approve` / `/photo-approve`).

## Empirical dispatch verification

Run against this machine's real installed Hermes (`~/.hermes/hermes-agent`,
v0.20.5), not assumed by analogy to `check-approval`'s prior verification:

```
>>> scan_skill_commands() registered fieldkit commands:
  /photo-approve -> .../platform/photo-agent/skills/photo-approve/SKILL.md
  /photo-reject  -> .../platform/photo-agent/skills/photo-reject/SKILL.md
  /process-photos -> .../platform/photo-agent/skills/process-photos/SKILL.md

>>> resolve_skill_command_key("photo_approve") = /photo-approve
>>> resolve_skill_command_key("photo_reject")  = /photo-reject

>>> resolve_command("approve") = CommandDef(name='approve', description='Approve a
    pending dangerous command', category='Session', gateway_only=True, ...)
>>> resolve_command("reject")  = None
```

This confirms, against Hermes's real source rather than its documented
behavior: `photo-approve` and `photo-reject` both register cleanly with no
collision; the Telegram-facing underscored forms both resolve to the
correct skill file; and the `approve` collision this whole rename exists to
avoid is real and reproducible, not a one-off.

Automated coverage exercising this same real dispatch-resolution code (not
mocked) lives in `platform/photo-agent/tests/test_photo_approve_dispatch.py`
and `test_photo_reject_dispatch.py` — 13 tests, all passing as of this
writing, covering: command-key registration, hyphen/underscore resolution,
frontmatter-name consistency, the abandoned `approve`/`check-approval`
command keys no longer resolving to anything, and (belt-and-suspenders) a
live re-assertion that `/approve` still collides with a core command so
this naming decision doesn't go stale silently if a future Hermes upgrade
ever frees it up. Structural/self-consistency coverage (SKILL.md ↔
`check_approval.py` contract, no stale button references, correct
underscored command referenced in the visible instructions) lives in
`test_photo_approve_skill.py` / `test_photo_reject_skill.py`. Full test
suite: 407 passed (`platform/photo-agent`), 84 passed (`platform/email-agent`)
as of this writing.

## What this supersedes

Both retired, not deleted — see the superseded banners added to each doc
for the specific mapping from old to new:

- **[`04-check-approval-skill.md`](04-check-approval-skill.md) (issue #8).**
  The `check-approval` skill it documents is renamed to `photo-approve`;
  its finding that Hermes cannot dispatch off a raw button-tap
  `callback_query` still holds and is exactly why this issue removes the
  buttons entirely rather than continuing to route around that limitation.
- **[`07-callback-race-fix.md`](07-callback-race-fix.md) (issue #29).** The
  dual-bot-token architecture it implements (`TELEGRAM_APPROVAL_BOT_TOKEN`)
  is retired — the second poller it protected from a `getUpdates` offset
  race no longer exists, so there's nothing left to protect.
- **Issue #31 / PR #42's fast-ack redesign** (long-poll + immediate
  `answer_callback_query`, `check_approval.py`'s former
  `_LONG_POLL_TIMEOUT_SECONDS`). Retired along with the rest of the cron
  leg — the race it shrank no longer exists because the thing that raced
  (the button-tap `callback_query`) no longer exists.
- **[`05-cron-verification.md`](05-cron-verification.md) (issue #13).**
  Partially superseded — its `check_approval.py`'s cron leg" coverage
  describes a poller and crontab entry that no longer apply.
  `check_email.py`'s and `upload_facebook.py`'s cron entries, also covered
  by that doc, are unaffected.

## Live-migration guide for already-deployed clients (`_demo`, `mercury`)

**Not performed by this PR** — explicitly out of scope, same pattern as the
OpenClaw removal (issue #14, see
[`06-openclaw-removal.md`](06-openclaw-removal.md)): a human walks through
this live on the Mac Mini with orchestrator guidance, after this PR merges.
`venus` and `_construction_co` are not yet live and need no migration —
their scaffolding (`.env.example`, `README.md`, `.specify/constitution.md`)
is already updated by this PR to the new single-bot model for their
eventual first setup.

### 1. Crontab — remove the `check_approval.py` entry

The live crontab currently runs (confirmed via `crontab -l` on 2026-08-26):

```
* * * * * env PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin bash -c '/usr/local/bin/python3 /Users/sandeep_a_k/src/fieldkit/platform/photo-agent/scripts/check_approval.py --source cron' >> /Users/sandeep_a_k/src/fieldkit/logs/cron.log 2>&1
* * * * * env PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin bash -c '/usr/local/bin/python3 /Users/sandeep_a_k/src/fieldkit/platform/photo-agent/scripts/upload_facebook.py --source cron' >> /Users/sandeep_a_k/src/fieldkit/logs/cron.log 2>&1
```

**This is not optional cleanup — it will start failing loudly the moment
this PR's code is deployed to the Mac Mini.** `check_approval.py` no longer
accepts `--source cron` at all (`--callback-data` is now `required=True`,
`choices=["approve", "reject"]`); every cron tick will exit via argparse's
usage error until the entry is removed. Remove the first line
(`check_approval.py --source cron`) with `crontab -e`. **Leave the second
line (`upload_facebook.py --source cron`) untouched** — that pipeline is
unaffected by this issue.

### 2. `.env` — remove `TELEGRAM_APPROVAL_BOT_TOKEN`

In each live client's `clients/{_demo,mercury}/src/photo-agent/.env`,
delete the `TELEGRAM_APPROVAL_BOT_TOKEN` line. `TELEGRAM_BOT_TOKEN` (the
existing Hermes gateway bot) now also serves the approval flow — no new
value is needed there. Do **not** delete the old approval bot's BotFather
registration yet if it's easy to keep around for a rollback window; there's
no harm in an unused bot token existing, only in a client `.env` still
declaring a variable the code no longer reads.

### 3. Hermes skill config — no change expected, but verify

`skills.external_dirs` already points at
`platform/photo-agent/skills` (unchanged directory, per
[`03-process-photos-skill.md`](03-process-photos-skill.md) /
[`04-check-approval-skill.md`](04-check-approval-skill.md)) — the rename
from `check-approval/` to `photo-approve/` plus the new `photo-reject/`
subdirectory are picked up automatically by the existing directory scan, no
config edit needed. Confirm post-deploy with:

```bash
hermes skills list --source local
```

Expect `photo-approve` and `photo-reject` listed as `local` / `enabled`,
and `check-approval` **absent** (confirms the rename replaced the old
command rather than adding a new one alongside it — same check
`test_photo_approve_dispatch.py::test_check_approval_command_key_no_longer_resolves`
makes automatically). If `check-approval` still appears, the deployed repo
checkout is stale — re-pull before continuing.

### 4. Live verification

1. Restart is **not** required for the crontab or `.env` edits above (cron
   re-reads its table on every tick; `check_approval.py` re-reads `.env` on
   every invocation) — but restart Hermes's gateway
   (`launchctl kickstart -k gui/501/ai.hermes.gateway`) after the `.env`
   edit anyway, since Hermes's own env loading happens at gateway start,
   not per-message.
2. Trigger a real approval message (`/process_photos project=<name>` via
   Hermes, or wait for a natural one) and confirm the message text reads
   "Reply /photo_approve or /photo_reject." with **no inline buttons**.
3. Reply `/photo_approve` (or `/photo_reject`) in the same Telegram chat.
   Confirm the expected downstream effect (approval email sent / Drive file
   deleted, per `logs/photo-agent.log`) and that `logs/cron.log` shows
   no further `check_approval.py` entries at all after the crontab edit.
4. Confirm `pending_approval` clears in `state.json` and (for approve) a
   Facebook upload is enqueued if `FB_PAGE_ID` is configured.
5. Repeat once more to rule out a one-off — same "repeat once" bar
   `07-callback-race-fix.md`'s own verification steps set.

### 5. Retiring the old approval bot (optional, once confident)

Once the above is confirmed stable, the old dedicated approval bot
registration can be deleted via [@BotFather](https://t.me/BotFather)
(`/deletebot`) if desired — purely cosmetic cleanup, not required for
correctness, since nothing in the codebase references
`TELEGRAM_APPROVAL_BOT_TOKEN` anymore once step 2 is done.
