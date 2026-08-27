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

> **Context (issue #61):** this doc's cutover checklist below occasionally
> references "a non-default Hermes profile, e.g. mercury" as a possibility
> to account for. As of issue #61, this project runs exactly one client at
> a time via Hermes's default profile only — see
> [`09-per-client-model-profiles.md`](09-per-client-model-profiles.md). A
> non-default profile directory may still exist on a machine as pre-#61
> leftover state (not yet retired), which is why the checklist below still
> handles that case, but standing up a *new* non-default profile is no
> longer this project's supported way to run a client.

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
  Partially superseded — its `check_approval.py`'s cron leg coverage
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

**Why the order below matters — read before starting.** Deploying this
PR's code while the old `check_approval.py --source cron` crontab entry is
still in place produces a *repeated, ongoing* failure: `check_approval.py`
no longer accepts `--source cron` at all (`--callback-data` is now
`required=True`, `choices=["approve", "reject"]`), so every cron tick exits
via argparse's usage error, forever, until the entry is removed. Removing
the cron entry *substantially* before deploying the new code leaves the old
button-tap flow with no consumer at all for however long that gap lasts (no
cron leg to catch a tap; Hermes never dispatches on `callback_query`
either — confirmed in `04-check-approval-skill.md`). The fix is not a
different order so much as a **short, uninterrupted window**: do steps 1–2
back-to-back in one sitting, with no approval expected to land mid-cutover
(step 0 checks for that), so the gap where neither the old button flow nor
the new command flow has a working consumer is seconds long, not hours.

### Cutover checklist (follow in order — do not skip or reorder)

0. **Confirm no approval is currently pending**, on the Mac Mini, for the
   client being migrated — `state.json` holds no secrets, so reading it
   directly is fine (unlike a `.env` file, see step 3.1):
   ```bash
   jq .pending_approval clients/_demo/data/photo-agent/state.json
   ```
   Expect `null`. If a real approval is pending, resolve it first (approve
   or reject via the *current*, pre-migration flow — tap the button or run
   `check_approval.py --callback-data approve`) before continuing. Starting
   the cutover with a pending approval in flight is exactly the scenario
   the "why the order matters" note above is warning about.

1. **Stop/remove the `check_approval.py` cron entry.** The live crontab
   currently runs (confirmed via `crontab -l` on 2026-08-26):
   ```
   * * * * * env PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin bash -c '/usr/local/bin/python3 /Users/sandeep_a_k/src/fieldkit/platform/photo-agent/scripts/check_approval.py --source cron' >> /Users/sandeep_a_k/src/fieldkit/logs/cron.log 2>&1
   * * * * * env PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin bash -c '/usr/local/bin/python3 /Users/sandeep_a_k/src/fieldkit/platform/photo-agent/scripts/upload_facebook.py --source cron' >> /Users/sandeep_a_k/src/fieldkit/logs/cron.log 2>&1
   ```
   Remove **only** the first line (`check_approval.py --source cron`) via
   `crontab -e`. **Leave the second line (`upload_facebook.py --source
   cron`) untouched** — that pipeline is unaffected by this issue.

2. **Deploy the new code** (`git pull` / whatever sync mechanism the Mac
   Mini uses to pick up this PR's merge commit on `main`), immediately
   after step 1, same sitting.

3. **Verify and consolidate Telegram tokens — in this exact sub-order:**
   1. **First**, verify `TELEGRAM_BOT_TOKEN` in the client's
      `clients/{_demo,mercury}/src/photo-agent/.env` is already the *same*
      token as that client's Hermes gateway profile uses (they're supposed
      to already match — this step exists to confirm that assumption
      before acting on it, not to assume it silently). **Compare hashes;
      never print either token.** A length mismatch proves the tokens
      differ — cheap, worth checking first — but equal length does **NOT**
      prove they match (two different Telegram tokens routinely have the
      same length); only equal hashes do. Treat length as a fast
      initial-mismatch check only, never as proof of a match:
      ```bash
      # Fast initial-mismatch check (equal length proves NOTHING — only
      # a length DIFFERENCE is meaningful here):
      grep '^TELEGRAM_BOT_TOKEN=' clients/_demo/src/photo-agent/.env | cut -d= -f2- | tr -d '\n' | wc -c
      grep '^TELEGRAM_BOT_TOKEN=' ~/.hermes/.env | cut -d= -f2- | tr -d '\n' | wc -c
      #   (~/.hermes/profiles/<profile>/.env for a non-default profile, e.g. mercury)

      # The actual proof of a match — hash equality, nothing less. Uses
      # `shasum -a 256` (ships with every macOS install) rather than
      # `sha256sum` (GNU coreutils, not guaranteed present on a stock Mac).
      # `cut -d= -f2-` (not `awk -F=`) takes everything after the FIRST
      # `=` so an `=` inside the token value itself wouldn't silently
      # truncate it. `tr -d '\n'` strips the trailing newline `cut`'s
      # output carries so the hash is computed on the exact token bytes
      # only — a stray trailing newline in one file but not the other
      # would otherwise produce a false mismatch between two tokens that
      # actually are equal.
      grep '^TELEGRAM_BOT_TOKEN=' clients/_demo/src/photo-agent/.env | cut -d= -f2- | tr -d '\n' | shasum -a 256
      grep '^TELEGRAM_BOT_TOKEN=' ~/.hermes/.env | cut -d= -f2- | tr -d '\n' | shasum -a 256
      ```
      If the lengths differ, the tokens are **definitely** different —
      stop here. If the lengths match, that alone proves nothing either
      way — the hash comparison is the only step that can actually confirm
      a match, and is required before proceeding regardless of what the
      length check showed. If the hashes don't match, **stop here** — do
      not proceed to step 3.2. That means the client's `.env` and its
      Hermes profile are already on two different bots today, which is a
      pre-existing misconfiguration this migration would otherwise
      silently paper over (both bots happen to work independently right
      now; consolidating onto the wrong one would break the gateway, the
      approval flow, or both). Reconcile which token is correct before
      continuing.
   2. **Only once 3.1 confirms a match**, delete the
      `TELEGRAM_APPROVAL_BOT_TOKEN` line from that same `.env` file.
      `TELEGRAM_BOT_TOKEN` now also serves the approval flow — no new
      value is needed.
   3. **Do not delete or revoke the old approval bot's BotFather
      registration yet.** Keep it registered for a defined rollback
      window (a few days of live use through the new flow is a reasonable
      bar) before retiring it via step 7 below — there's no cost to an
      unused bot token existing, only to a client `.env` still declaring a
      variable the code no longer reads (which step 3.2 already handled).

4. **Restart Hermes's gateway** so it picks up the renamed/new skill files
   from step 2. `agent.skill_commands.get_skill_commands()` caches its
   skill-command map in memory and only rescans automatically when the
   active platform or profile home changes (`agent/skill_commands.py`)
   — a plain on-disk rename doesn't invalidate that cache by itself. A
   fresh gateway process has an empty cache and rescans on first access, so
   a restart is a reliable way to pick this up; `hermes`'s own
   `/reload-skills` command is a lighter-weight alternative that rescans
   without restarting the process, if preferred.

   **Not** about the `.env` edit from step 3 — Hermes reads its *own*
   profile's env (`~/.hermes/.env` for the default profile, or
   `~/.hermes/profiles/<profile>/.env`), never the client's
   `photo-agent/.env`, which is a completely separate consumer read only by
   the plain Python scripts (`check_approval.py`, `process_photos.py`, ...).
   Step 3 only *compares against* the Hermes profile's env — it never
   writes to it — so there is nothing from step 3 for a restart to pick up.

   > **Under issue #61's single-install architecture (see
   > [`09-per-client-model-profiles.md`](09-per-client-model-profiles.md)),
   > do NOT restart `ai.hermes.gateway-mercury` (or any other non-default
   > profile's gateway) as a way to pick up this migration for that
   > client.** This project runs exactly one client at a time via the
   > default profile only — a client that needs this migration should
   > already be, or should first be made, the one client installed via
   > `platform/photo-agent/scripts/install_client.sh <client>`, and it's
   > `ai.hermes.gateway` (the default profile's label) that needs
   > restarting for it, not a named per-client label. Restarting a
   > leftover named profile's gateway instead is exactly the kind of
   > action that keeps two gateways with two different clients' live
   > credentials running simultaneously — the same exposure issue #59 and
   > #61 exist to close — confirmed live on this machine via `launchctl
   > list | grep hermes`, which showed both `ai.hermes.gateway` (default)
   > and `ai.hermes.gateway-mercury` running as separate services
   > simultaneously. The paragraph and command below are left as
   > historical record of how per-profile restarts worked under the
   > retired concurrent-profile model — not a live instruction.

   The launchd service label used to be **profile-specific, not
   universal**, under that retired model — confirmed directly in Hermes's
   own source (`hermes_cli/gateway.py::get_launchd_plist_path()`: "Default
   `~/.hermes` → `ai.hermes.gateway.plist` (backward compatible). Profile
   `~/.hermes/profiles/coder` → `ai.hermes.gateway-coder.plist`."): the
   default profile uses bare `ai.hermes.gateway`, and every other profile
   got `ai.hermes.gateway-<profile>` (e.g. `ai.hermes.gateway-mercury`).
   Under the current architecture there is only ever the default profile:
   ```bash
   launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway
   ```
   (The crontab and code-deploy steps above don't need a restart on their
   own — cron re-reads its table every tick, and `check_approval.py`
   re-reads `.env` on every invocation.)

5. **Verify the skill rename landed**, before doing a live approval test:
   ```bash
   hermes skills list --source local
   ```
   Expect `photo-approve` and `photo-reject` listed as `local` / `enabled`,
   and `check-approval` **absent** (confirms the rename replaced the old
   command rather than adding a new one alongside it — same check
   `test_photo_approve_dispatch.py::test_check_approval_command_key_no_longer_resolves`
   makes automatically). If `check-approval` still appears, the deployed
   repo checkout is stale — re-pull (repeat step 2) before continuing.
   `skills.external_dirs` itself needs no config change — it already
   points at `platform/photo-agent/skills`
   ([`03-process-photos-skill.md`](03-process-photos-skill.md) /
   [`04-check-approval-skill.md`](04-check-approval-skill.md)) — but the
   rename is only picked up by a fresh scan of that directory, which is
   exactly what step 4's restart (or `/reload-skills`) forces; this step
   is where that actually gets confirmed.

6. **Live verification:**
   1. Trigger a real approval message (`/process_photos project=<name>` via
      Hermes, or wait for a natural one) and confirm the message text reads
      "Reply /photo_approve or /photo_reject." with **no inline buttons**.
   2. Reply `/photo_approve` (or `/photo_reject`) in the same Telegram chat.
      Confirm the expected downstream effect (approval email sent / Drive
      file deleted, per `logs/photo-agent.log`) and that `logs/cron.log`
      shows no further `check_approval.py` entries at all after step 1's
      crontab edit.
   3. Confirm `pending_approval` clears in `state.json` and (for approve) a
      Facebook upload is enqueued if `FB_PAGE_ID` is configured.
   4. Repeat once more to rule out a one-off — same "repeat once" bar
      `07-callback-race-fix.md`'s own verification steps set.

7. **Retire the old approval bot, once the rollback window from step 3.3
   has passed with the new flow confirmed stable.** Delete it via
   [@BotFather](https://t.me/BotFather) (`/deletebot`) if desired — purely
   cosmetic cleanup at this point, not required for correctness, since
   nothing in the codebase references `TELEGRAM_APPROVAL_BOT_TOKEN` anymore
   once step 3.2 is done.
