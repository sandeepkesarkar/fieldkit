# Hermes-vs-Cron `getUpdates` Offset Race — Fix (issue #29)

Closes issue #29: `check_approval.py`'s cron leg and Hermes's gateway were
both polling Telegram `getUpdates` against the same bot token, so Hermes —
polling continuously — would consume and offset-past a real Approve/Reject
button tap before the cron leg's once-a-minute run ever saw it. Confirmed a
guaranteed race, not a probabilistic one (see `05-cron-verification.md`'s
"Acceptance Criteria — Final Status" item 4 and `06-openclaw-removal.md`'s
SC-001 section for the prior tracking).

Source: [`platform/.specify/003-hermes-runtime/spec.md`](../../.specify/003-hermes-runtime/spec.md)
(FR-002a, FR-003).

## The race, restated

`python-telegram-bot`'s `Updater._start_polling` (Hermes's own dependency)
unconditionally advances the shared per-token offset for every update it
receives, whether or not its own handler recognizes the `callback_data`.
Hermes's Telegram adapter cannot dispatch a skill off FieldKit's raw
`approve`/`reject` `callback_data` (FR-002a, confirmed empirically against
`plugins/platforms/telegram/adapter.py::_handle_callback_query`), so every
button tap Hermes consumes is a tap the cron leg never gets a chance to
process — advanced past, not merely delayed.

## Options evaluated

Re-verified directly against this machine's installed Hermes source
(`~/.hermes/hermes-agent`), not just against the issue's description:

1. **Hermes-side recognition of `approve`/`reject` (issue's option b).**
   `_handle_callback_query` matches a hardcoded, closed prefix set — read out
   in full from the installed source rather than sampled: `mp:`, `mpg:`,
   `mpv:`, `mm:`, `mc:`, `mb`, `mx`, `mg:`, `cp:`, `gt:`, `ea:`, `sc:`, `cl:`,
   `update_prompt:`. None of them match FieldKit's bare `"approve"`/`"reject"`
   payloads. Making the handler recognize them means editing that function in
   an installed dependency, not a fieldkit-owned file.
2. **Moving button-callback handling into a Hermes skill (issue's option c).**
   Hermes skills are Markdown files dispatched off slash commands / free-text
   matching (`agent/skill_commands.py::scan_skill_commands()`); there is no
   skill-level hook for a raw `callback_query` update at all.
   `_normalize_platform_event`, Hermes's only generic inbound-event hook,
   returns `None` for `callback_query`. Hermes's own
   `docs/relay-connector-contract.md` documents the identical posture on its
   newer relay/connector path by design: "Foreign callback payloads (another
   integration's buttons) never become prompt events... dropped at the
   connector." Both the direct-adapter path and the relay path independently
   confirm this is structural, not a configuration gap — same conclusion
   FR-002a already reached, re-verified here rather than assumed.
3. **A fourth option, not in the original issue text: filter Hermes's own
   `getUpdates` to exclude `callback_query`.** Checked
   `plugins/platforms/telegram/adapter.py` directly: `allowed_updates=Update.ALL_TYPES`
   is hardcoded at both call sites (`_start_polling_once`, and the parallel
   path further down the file) — not exposed via `config.yaml` or
   `plugin.yaml`, so this alone already requires patching Hermes's installed
   adapter code, same conclusion as (1) and (2). But even setting that aside,
   filtering would not actually have made the cron leg "the safe sole
   consumer" of `callback_query`, because Telegram's `getUpdates` offset is a
   single monotonic counter **per bot token**, not per update type and not
   per polling client — there is no way for two independent consumers of one
   bot's update stream to hold separate offsets. If Hermes's own `getUpdates`
   call later advanced past a *filtered-out* callback's `update_id` (which it
   would, the moment any later, allowed-type update arrived and Hermes
   confirmed receipt up to that higher offset), Telegram would drop the
   earlier callback from the stream permanently — for every consumer of that
   token, cron leg included. Filtering by update type changes *which* updates
   Hermes acts on; it does not give the cron leg an independent offset. The
   race would just become collateral instead of direct.
4. **A second, dedicated bot token for the button-callback surface (issue's
   option a).** Fully achievable inside fieldkit's own repo/config — no
   Hermes patch, no dependency on Hermes's internals at all. Cost: the
   admin ends up with two bot identities in Telegram, since the button-
   bearing approval message must be sent by (and can only be polled/answered
   by) the same token that will later see the tap.

Options 1–3 all resolve to the same structural fact FR-002a already
established: **Hermes cannot be taught to handle FieldKit's own callback
payload without patching an installed dependency.** That's a real
architecture fork against option 4 (stay within fieldkit's own scope vs.
maintain a Hermes fork across upgrades) — put to the repo owner via
`AskUserQuestion` rather than decided unilaterally. **Decision: option 4
(dedicated second bot token).**

## Implementation

The entire button-callback surface — not just the `getUpdates` poll — now
runs on a second bot token, `TELEGRAM_APPROVAL_BOT_TOKEN`, kept separate
from Hermes's `TELEGRAM_BOT_TOKEN`:

- **`platform/photo-agent/tools/telegram_api.py`** — every function
  (`send_message`, `send_message_with_buttons`, `answer_callback_query`,
  `edit_message_reply_markup`, `get_updates`) now accepts an optional
  `token_env_var` (default `"TELEGRAM_BOT_TOKEN"`, unchanged for every
  caller that doesn't pass it) instead of hardcoding the env var name.
- **`process_photos.py`** — `send_message_with_buttons` (the approval
  message with its Approve/Reject inline keyboard) now passes
  `token_env_var="TELEGRAM_APPROVAL_BOT_TOKEN"`. This message has to be sent
  by the approval bot, because Telegram only ever routes a button tap's
  `callback_query` back to the bot that sent the original message —
  whichever token sends the message is the only token that can later poll,
  answer, or edit it.
- **`check_approval.py`** — the entire button-callback flow uses the
  approval token: `get_updates` (cron path), `answer_callback_query` (both
  the cron path's direct call and the direct path's `_acknowledge_tap`),
  `edit_message_reply_markup` (`_remove_buttons`, both paths), and
  `_notify_admin`'s outcome messages. Keeping `_notify_admin` on the
  approval bot too (rather than splitting it back to the primary bot) keeps
  the whole approve/reject interaction — button, tap acknowledgement, and
  outcome — in one Telegram conversation.
- **What deliberately stayed on `TELEGRAM_BOT_TOKEN`:** Hermes's own
  gateway traffic (unaffected — a different process entirely), and
  `process_photos.py`'s `_telegram_error()` pipeline-failure notifications
  (`❌ Drive upload failed`, etc.) — these aren't part of the button-callback
  race and relaying them through Hermes's usual bot keeps error visibility
  consistent with every other Hermes-relayed message.
- **`.env.example`** updated in `platform/photo-agent/`,
  `clients/_demo/src/photo-agent/`, and
  `clients/_construction_co/src/photo-agent/` to document the new required
  variable and why it must be a *different* bot registration from
  `TELEGRAM_BOT_TOKEN`.
- **Equal-token guard (`tools/telegram_api.py::_token()`).** The token split
  only closes the race if the two env vars actually hold different values.
  An operator who copies `TELEGRAM_BOT_TOKEN`'s value into
  `TELEGRAM_APPROVAL_BOT_TOKEN` (e.g. while filling in `.env` quickly) would
  otherwise run without any error while silently recreating the exact
  shared-offset race this whole fix exists to eliminate — nothing about a
  same-value token would look wrong until a real button tap failed to be
  processed. `_token()` now raises immediately, before any HTTP call, if a
  non-default `token_env_var` resolves to the same value as
  `TELEGRAM_BOT_TOKEN`. Covered by `test_telegram_api.py`'s
  `test_approval_token_equal_to_primary_token_raises` and its
  before-any-request variant.

Why this actually closes the race: Hermes's `getUpdates` long-poll and
`check_approval.py`'s `getUpdates` poll now run against two different bot
tokens. Telegram maintains the update offset **per bot token**, not per
chat or per admin — two different tokens simply never contend for the same
offset, regardless of polling frequency. This isn't a mitigation of the
race's odds; it removes the shared resource the race was over.

## Setup step required on the Mac Mini (not done by this PR)

This PR ships the code and docs; it does not and cannot create a live
Telegram bot or edit the live `.env` on the Mac Mini. Before this fix is
live, a human must:

1. Register a second bot via [@BotFather](https://t.me/BotFather) (`/newbot`)
   — any name/username, distinct from the existing FieldKit bot.
2. Add `TELEGRAM_APPROVAL_BOT_TOKEN=<new token>` to the live
   `clients/_demo/src/photo-agent/.env` (and `_construction_co`'s, once that
   client is live).
3. From the admin's Telegram account, send `/start` to the new bot once —
   required before any bot can message a chat_id first. `ADMIN_TELEGRAM_CHAT_ID`
   does **not** need to change: for a private DM, Telegram's `chat_id` is the
   admin's own user id, the same value regardless of which bot the
   conversation is with.
4. Restart the cron leg is not required (it's invoked fresh every minute by
   cron and re-reads `.env` each run); no `launchd`/Hermes restart is needed
   either, since Hermes's own token and polling are untouched.

## Verification

**Automatically verified (this PR, no live Telegram involved):**
- `tests/test_telegram_api.py` — every function's default `token_env_var`
  stays `"TELEGRAM_BOT_TOKEN"` (no behavior change for existing callers), and
  a new set of tests asserts that passing `token_env_var="TELEGRAM_APPROVAL_BOT_TOKEN"`
  reads that env var instead — including the failure path (raises with the
  *passed* var's name when unset) and `_redact_token` redacting the correct
  token per call.
- `tests/test_check_approval.py` — new assertions on top of the existing
  approve/reject/cron-path tests confirming `get_updates`,
  `answer_callback_query`, `edit_message_reply_markup`, and `_notify_admin`'s
  `send_message` are all invoked with
  `token_env_var="TELEGRAM_APPROVAL_BOT_TOKEN"`, on both the cron path and
  the direct (Hermes-invoked) path.
- `tests/test_process_photos.py` — new assertion that
  `send_message_with_buttons` is invoked with
  `token_env_var="TELEGRAM_APPROVAL_BOT_TOKEN"`, and that `_telegram_error`'s
  `send_message` calls are NOT (i.e. stay on the default/primary token).
- Full existing suite re-run green (see PR description for the count) —
  confirms this change doesn't regress any other flow.

**Live-verified — 2026-08-23/24, Mac Mini, `_demo` client.** Requires a live
human check, same posture as issue #7's dispatch-coverage precedent: whether
a *real* Telegram button tap is correctly handled while both
`ai.hermes.gateway` and the cron leg are running simultaneously against the
live bots. Unit tests mock the Telegram HTTP layer entirely and cannot
observe Telegram's own per-token offset bookkeeping, which is the actual
mechanism the race depended on and the fix removes.

**Setup confirmed:** `ai.hermes.gateway` running continuously throughout
(pid 18785, uptime unbroken across the whole test — verified before and
after); `crontab -l` showed the single `check_approval.py --source cron`
entry, once a minute, no duplicates. `TELEGRAM_APPROVAL_BOT_TOKEN` and
`TELEGRAM_BOT_TOKEN` confirmed present and distinct (46 chars each) in the
live `clients/_demo/src/photo-agent/.env`.

**Run:** `scripts/run_e2e_test.py --duration 6` generated a real 2-photo
test project (`e2e-test-20260824-024517`), uploaded it to Drive, and sent a
real Telegram approval message via the dedicated approval bot
(`APPROVAL_REQ project=e2e-test-20260824-024517 message_id=4` in the
activity log) — confirmed arriving on the new approval bot, separate from
Hermes's own bot conversation.

**Three real button-tap callback events were generated and processed by the
cron leg during this run**, `ai.hermes.gateway` running the entire time:

1. **Approve tap (accidental), attempt 1** — `cron.log`:
   `ERROR:__main__:answer_callback_query failed: Telegram HTTP error 400: Bad
   Request: query is too old and response timeout expired or query ID is
   invalid`. Cron *did* find and attempt to process the tap (this is an
   explicit error, not a silent skip) — it lost a race against Telegram's
   own callback-freshness window, not against Hermes. Tracked separately as
   issue #31 (cron cadence vs. Telegram's callback-answer window) — out of
   scope for this fix.
2. **Approve tap, attempt 2 (Telegram redelivery)** — same `query is too
   old` error, same cause.
3. **Reject tap (deliberate retry)** — processed cleanly, no error. Activity
   log: `2026-08-24 02:53 | REJECTED | project=e2e-test-20260824-024517`.
   Full clean flow: Drive video file deleted, admin notified via the
   approval bot, `pending_approval` cleared, `telegram_update_offset`
   advanced.

**The regression check, for all three events:** `grep` across the entire
`~/.hermes/logs/gateway.log` for this project name, `callback_query`, or
`message_id=4` — **zero matches**, at any point. `grep` for `409`/`Conflict`
restricted to the actual test window (`2026-08-24 02:40`–`02:59 UTC`) —
**zero matches** (the log's only Conflict entries anywhere are from
`2026-08-21` and `2026-08-23 15:07 EDT`, hours before Hermes's last restart
at `21:33 EDT` that day and completely outside this test). Hermes never
consumed, delayed, or raced for any of these three callbacks — confirming
the two tokens never contended for the same `getUpdates` offset, which is
exactly what this fix guarantees.

**Facebook safety, confirmed throughout:** `facebook_state.json` showed
`pending_facebook_upload: null` and no key for this project's message_id at
every checkpoint, including immediately after both accidental Approve
attempts (which failed before `check_approval.py` ever reached the
`_enqueue_facebook_upload` call) and after the final Reject. No live
Facebook publish occurred at any point in this verification.

**Verdict: PASS.** Three independent callback events (not a single one-off)
processed by the cron leg with `ai.hermes.gateway` running continuously
throughout, zero footprint in Hermes's logs for any of them, zero `409`
conflicts in the test window. This satisfies the "repeat once to rule out a
fluke" bar in the original steps below.

**Side findings, tracked separately (out of scope for this fix):**
- **#31** — `check_approval.py`'s once-a-minute cron cadence can lose the
  race against Telegram's own `answerCallbackQuery` freshness window
  (attempts 1–2 above). Different mechanism from the Hermes/cron token race
  this doc fixes; the approval got stuck (`pending_approval` left set,
  offset already advanced past the stale callback) until the human tapped
  again.
- **#32** — `check_email.py`'s cron job is separately failing on an expired
  Gmail OAuth refresh token (`invalid_grant`) — unrelated agent, unrelated
  credential, surfaced incidentally while checking `cron.log` during this
  verification.

---

Original manual verification steps, preserved for reference / re-running if
the fix ever needs re-verification after a Hermes upgrade:

1. Confirm both are running: `launchctl list | grep ai.hermes.gateway` and
   `crontab -l | grep check_approval`.
2. Trigger a real approval message (`/process_photos project=<name>` via
   Hermes, or wait for a natural one) and confirm it arrives from the NEW
   approval bot, not the existing Hermes bot.
3. Tap **Approve** (or **Reject**) on that message.
4. Within the next minute, confirm: the tap's spinner clears and the
   buttons disappear (this alone shows *some* token answered the callback);
   the expected downstream effect happens (approval email sent / Drive file
   deleted, per `logs/cron.log` and the activity log); and — the actual
   regression check — `logs/cron.log` shows `check_approval.py`'s cron tick
   that processed the tap, not a "no matching callback" debug line for that
   run. A clean processed-tap entry, with Hermes's gateway logs
   (`~/.hermes/logs/gateway.log`) showing no related `409 Conflict` for this
   bot token during the same window, is the confirmation this doc asks for.
5. Repeat once more to rule out a one-off — the original bug reproduced
   "essentially every time," so a single clean run is suggestive but a
   second confirms it wasn't a fluke of timing.
