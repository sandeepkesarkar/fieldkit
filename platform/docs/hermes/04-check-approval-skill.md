# check-approval as a Hermes Skill (issue #8)

> **Superseded by issue #49 (2026-08-26).** The `check-approval` skill this
> doc describes was renamed to `photo-approve` and a new sibling
> `photo-reject` skill was added — see
> [`10-text-based-approval-migration.md`](10-text-based-approval-migration.md).
> This doc's empirical finding that Hermes cannot dispatch off the raw
> Approve/Reject button `callback_query` still holds and is exactly why
> issue #49 removed the buttons entirely rather than continuing to work
> around that limitation — that part of this doc is not obsolete, just
> superseded by a different resolution. Left in place as a historical
> record of the original manual-command port, not silently deleted.

Replaces OpenClaw's `SKILL_check_approval.md` with a Hermes-native skill at
`platform/photo-agent/skills/check-approval/SKILL.md`, covering the manual
`/check_approval` command trigger only. `check_approval.py` itself is
unchanged — this is a dispatch-layer swap for the reachable trigger, per
FR-002.

**This skill does NOT cover the Approve/Reject button-callback trigger.**
That's the headline difference from #7's `process-photos` port, and the
reason this issue needed a spec amendment
(`platform/.specify/003-hermes-runtime/spec.md` FR-002a) before
implementation rather than after. See "Why the button-callback trigger isn't
here" below.

## Setup: `skills.external_dirs`

Same discovery mechanism as `process-photos` (#7) — Hermes's
`skills.external_dirs` in `~/.hermes/config.yaml` already points at
`~/src/fieldkit/platform/photo-agent/skills`, this skill's parent directory,
so no config change was needed:

```yaml
skills:
  external_dirs:
    - ~/src/fieldkit/platform/photo-agent/skills
```

No copy step, no stale-cache risk, no live-config change required for this
issue — this file lands in an already-watched directory.

**Verification note:** unlike #18's `process-photos` doc, this pass did not
capture a live `hermes skills list --source local` run against the running
gateway. Temporarily repointing the live `~/.hermes/config.yaml` at this
issue's worktree (the same technique #18 used) was attempted and blocked by
this session's own safety tooling as a change to live infrastructure outside
the repo, appropriately so — it's a running service's config, and doing that
from an automated session isn't a well-scoped action for this issue.
Discovery is instead verified reproducibly and more strongly by
`test_check_approval_dispatch.py`, which runs Hermes's actual
`scan_skill_commands()` / `resolve_skill_command_key()` against this skill's
real files on disk under Hermes's own venv (no live-service mutation
needed). A live `hermes skills list` confirmation once this merges to `main`
(the directory `external_dirs` already watches) is a reasonable manual
follow-up, not a blocker.

## Naming: hyphen vs. underscore

Named `check-approval` (hyphenated) from the start — no rename step, unlike
#18's two-step `process_photos` → `process-photos` migration. The dispatch
mechanics are identical to #18's finding (`agent/skill_commands.py`
normalizes any frontmatter `name` to a hyphenated slug for its internal
command key regardless of underscore/hyphen source, and
`hermes_cli/commands.py::_sanitize_telegram_name()` converts it back to
underscores for the actual Telegram bot command) — reused rather than
re-verified from scratch, since it's the same Hermes source read for #18 and
this issue changes nothing about that mechanism. `test_check_approval_dispatch.py`
does independently confirm it against *this* skill's files (not just cite
#18's finding): both `resolve_skill_command_key("check_approval")` and
`resolve_skill_command_key("check-approval")` resolve to `/check-approval`,
scanned from this skill's actual `SKILL.md`.

## Why the button-callback trigger isn't here

The original issue text and `platform/.specify/003-hermes-runtime/spec.md`'s
FR-002 (pre-amendment) both assumed, by analogy with OpenClaw, that Hermes
could dispatch a skill directly off the Approve/Reject button tap the same
way it dispatches off a typed `/check_approval` command. That assumption
didn't survive contact with Hermes's actual source.

**Investigated, not assumed** — directly probed Hermes's own Telegram
adapter under its own venv, the same evidentiary bar #18 set for the naming
question:

```python
from plugins.platforms.telegram.adapter import TelegramAdapter
# ... construct adapter with mocked _bot/_app, mocked callback_query with
#     query.data = "approve"  (FieldKit's real button payload) ...
await adapter._handle_callback_query(update, context)
# query.answer.called       -> False
# query.edit_message_text.called -> False
```

`_handle_callback_query` (`plugins/platforms/telegram/adapter.py`) only
recognizes a closed, hardcoded set of Hermes-internal `callback_data`
prefixes:

| Prefix | Purpose |
|---|---|
| `mp:` / `mpg:` / `mpv:` / `mm:` / `mc:` / `mb` / `mx` / `mg:` | model picker |
| `cp:` | generic choice picker (`/reasoning`, `/fast`) |
| `gt:` | Gmail-triage |
| `ea:` | exec approval (Hermes's own dangerous-command gate) |
| `sc:` | slash-confirm |
| `cl:` | clarify picker |
| `update_prompt:` | self-update yes/no |

FieldKit's own Approve/Reject buttons
(`platform/photo-agent/tools/telegram_api.py::send_message_with_buttons`)
send bare `callback_data` of `"approve"` / `"reject"` — no prefix, matching
none of the above. Every branch falls through and the function returns
having done nothing: no `answer_callback_query` (so the Telegram client's
loading spinner just times out on its own), no `edit_message_text`, and —
the part that actually matters for skill dispatch — no call into any skill
invocation or agent-turn code path at all.

`_normalize_platform_event`, Hermes's only other generic inbound-event hook
(feeding the `gateway_platform_event` plugin observer hook), is wired for
exactly two update kinds:

```python
def _normalize_platform_event(self, update) -> Optional[Dict[str, Any]]:
    if getattr(update, "message_reaction", None) is not None:
        return self._normalize_reaction_event(update)
    if getattr(update, "edited_message", None) is not None:
        return self._normalize_message_edited_event(update)
    return None
```

`callback_query` isn't one of them — confirmed by probe (`None` returned for
an update carrying only a `callback_query`). So there is no alternate route
for a foreign `callback_data` to reach a skill, a plugin, or the LLM turn.

Hermes's own `docs/relay-connector-contract.md` (for the separate
multi-gateway relay deployment mode, not the local adapter used here)
documents the identical posture as an explicit design choice, not an
oversight: *"Foreign callback payloads (another integration's buttons)
never become prompt events... dropped at the connector."* Hermes's own
button-driven interactions (`ea:` exec approvals, `sc:` slash-confirms) use
a **gateway-minted, opaque prompt-id token scheme** (`hp1:<prompt_id>:<option_id>`
in the relay contract; `ea:<choice>:<id>` / `sc:<choice>:<id>` locally) —
by design, nothing outside Hermes itself can mint a token that dispatches
through this path. FieldKit's buttons were never going to qualify, no matter
how SKILL.md's prose was worded.

**No workaround pursued within this issue's scope.** Making
`"approve"`/`"reject"` dispatchable would require patching Hermes's adapter
itself — Hermes is an installed dependency (`~/.hermes/hermes-agent`, a
`git`-cloned application, not a fieldkit-owned file, and not the
`.agents/agent-dev-kit` submodule either), not something fieldkit patches.
Renaming FieldKit's own callback data to match one of Hermes's schemes
(e.g. an `ea:`-prefixed token) isn't viable either — the acceptance criteria
for this issue requires `check_approval.py` and its existing tests
untouched, and those schemes are gateway-minted and opaque by design, not
freely assignable by a caller.

## What actually handles the button tap

Unchanged from before this issue: `check_approval.py`'s cron leg (FR-003,
`* * * * * ... check_approval.py --source cron`) polls Telegram `getUpdates`
independently of Hermes and processes the Approve/Reject decision from the
update body directly, with no CLI callback-data argument involved. This was
already how `check_approval.py` was built (see its own module docstring:
"Cron path: reads state.json for a pending approval, polls Telegram
getUpdates ..."), and per FR-002a it's now documented as the sole mechanism
for the button-tap trigger, not an alternate path alongside a
Hermes-dispatched one.

**Known follow-up risk — confirmed during PR #21 review, not resolved by
this issue.** Telegram's Bot API allows only one active `getUpdates`
long-poll per bot token (this is exactly what forced OpenClaw's gateway to
be unloaded when Hermes's gateway went live in #6 — see
`02-gateway-setup.md`'s "single-poller conflict" note). `check_approval.py`'s
cron leg polls the same shared bot token Hermes's gateway now holds a
continuous long-poll on.

This is not a theoretical risk. Confirmed from `python-telegram-bot`
(Hermes's own dependency) source, `venv/lib/.../telegram/ext/_updater.py`,
`Updater._start_polling`'s `polling_action_cb`:

```python
if updates:
    ...
    for update in updates:
        await self.update_queue.put(update)
    self._last_update_id = updates[-1].update_id + 1  # Add one to 'confirm' it
```

The offset advance is unconditional — it happens for every update PTB
receives, whether or not any handler (including `_handle_callback_query`
above) recognizes or acts on it. Telegram's per-token offset is shared
across every caller; whichever poller's `getUpdates` call returns first
"confirms" (and removes) that update for everyone. Hermes polls
continuously; `check_approval.py`'s cron leg polls once a minute. Once both
are active on the same token, Hermes will consume and offset-past a
button-tap callback before the cron leg's next run can see it, essentially
every time — not occasionally. Concurrent overlapping calls can also
produce a `409 Conflict` outright.

**Live evidence gathered during review (2026-08-21):**

- `~/.hermes/logs/gateway.log` was cycling through `409 Conflict: terminated
  by other getUpdates request` roughly every 25 seconds, in real time,
  during the review.
- That specific conflict is *not* currently `check_approval.py`'s cron leg
  vs. Hermes, though: `logs/cron.log` shows every cron invocation failing at
  the OS level —
  `can't open file '.../clients/_demo/src/photo-agent/scripts/check_approval.py'`
  — because the crontab entry still points at the pre-Feature-002-migration
  path. The cron leg never reaches `getUpdates` right now; a separate,
  pre-existing bug this issue did not introduce.
- `launchctl list` shows `ai.openclaw.gateway` still loaded alongside
  `ai.hermes.gateway` right now, contradicting `02-gateway-setup.md`'s claim
  that OpenClaw was unloaded during #6. The live 409s are almost certainly
  Hermes vs. OpenClaw, not Hermes vs. the cron leg.

So FR-002a's premise — that the cron leg keeps handling the button tap
"unchanged" — does not currently hold in practice, for two independent
pre-existing reasons, and would still face the guaranteed PTB-offset race
against Hermes even once the crontab path is fixed. None of this was
introduced by #8, and fixing live crontab/launchd state is outside a
git-tracked file and outside this issue's scope — not attempted here.

**Decision (admin, PR #21 review thread, 2026-08-21):** FieldKit is moving
to stop depending on OpenClaw entirely rather than design a long-term
shared-bot-token arrangement between it and Hermes. This is already the
existing dependency chain: #13 (verify the cron-triggered scripts, including
`check_approval.py`'s cron leg, actually run — its acceptance criteria would
currently fail on both findings above) gates #14 (uninstall OpenClaw from
the Mac Mini). No new issues opened; findings posted to #13 instead. The
PTB-offset race against Hermes itself (independent of OpenClaw) is a
separate, still-open question #13 should also verify once the crontab path
is corrected — uninstalling OpenClaw alone does not resolve it, since Hermes
would still be the sole continuous poller racing the cron leg.

## OpenClaw → Hermes mapping

Full reasoning lives as an HTML comment at the top of the skill file itself
(so it travels with the file, not just this doc). Summary:

- **Slash command**: no more `user-invocable: true` frontmatter field —
  every installed skill's `name` is automatically a slash command in Hermes.
  Named the skill `check-approval` (hyphenated, agentskills.io-compliant
  from the start); the Telegram command the admin types stays
  `/check_approval`.
- **Prerequisites**: `prerequisites.commands: [python3]` (informational) —
  this skill has no binary prerequisites beyond `python3` (unlike
  `process-photos`, which also needs `ffmpeg`/`gws`), so no `which` checks
  are needed in the body.
- **Trigger scope reduced**: OpenClaw's `SKILL_check_approval.md` had three
  trigger sections (Approve button, Reject button, manual command). This
  skill has one (manual command) — see "Why the button-callback trigger
  isn't here" above.
- **Everything else** (script invocation with `--callback-data approve`,
  verbatim-relay instructions, no-retry instruction) is unchanged text for
  the trigger that *is* ported.

## Verification

Two layers of automated coverage, plus one honest gap:

- `platform/photo-agent/tests/test_check_approval_skill.py` — structural
  consistency (frontmatter name, prerequisites, the skill body only ever
  invokes `--callback-data approve` and never `reject`, verbatim-relay and
  no-retry instructions present, the old OpenClaw `SKILL_check_approval.md`
  is actually gone).
- `platform/photo-agent/tests/test_check_approval_dispatch.py` — real
  dispatch-path coverage for BOTH triggers named in the issue, with opposite
  expected outcomes:
  - Manual command: Hermes's real `scan_skill_commands()` /
    `resolve_skill_command_key()` register this file under `/check-approval`
    and resolve both `check_approval` and `check-approval` input to it.
  - Button callback: Hermes's real `_handle_callback_query`, invoked
    directly with FieldKit's actual `"approve"`/`"reject"` payloads, takes
    no action — the negative-result proof behind "Why the button-callback
    trigger isn't here" above, not just prose describing it.
- Manual LLM-driven verification (an LLM actually following the skill's
  prose against a live Hermes session, the way #18's doc did for
  `process-photos`) — **not done this pass.** Unlike #18, this issue's
  finding is about whether Hermes's *own dispatch layer* ever reaches the
  skill at all for the button-tap case (proven negatively, no LLM needed),
  and the manual-command case is mechanically identical to #18's already
  manually-verified `process-photos` flow (same `scan_skill_commands()` /
  `_build_skill_message()` machinery, different skill body). A live
  `/check_approval` Telegram round-trip once this merges is a reasonable
  manual follow-up, not a blocker for this issue's scope.

## Next steps

- Live `hermes skills list` confirmation once merged to `main` (see
  "Verification note" above)
- Live Telegram round-trip for the manual `/check_approval` command
- `#13` — verify cron-triggered scripts (including `check_approval.py`'s
  cron leg) actually run; per this issue's review findings, its acceptance
  criteria would currently fail (stale crontab path) and should also verify
  the PTB-offset race against Hermes once that's fixed
- `#14` — uninstall OpenClaw from the Mac Mini (gated on #13; admin decision
  during PR #21 review to stop depending on OpenClaw entirely rather than
  design a long-term shared-bot-token arrangement)
