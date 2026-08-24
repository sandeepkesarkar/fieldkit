# check-email as a Hermes Skill (issue #25)

Replaces OpenClaw's `platform/email-agent/SKILL.md` (frontmatter
`metadata: {"openclaw": {...}}`) with a Hermes-native skill at
`platform/email-agent/skills/check-email/SKILL.md`. `check_email.py` itself
is unchanged — this is a dispatch-layer swap only, per FR-002, same as #7's
`process-photos` and #8's `check-approval`.

This is the last of the three FieldKit skills OpenClaw ever hosted manual
commands for. Unlike `process-photos` (takes an argument) or
`check-approval` (has a Hermes-unreachable button-callback trigger), the
manual `/check_email` command takes no arguments and has no callback
surface — Telegram acknowledgements are outbound-only notifications, not
interactive messages. So this port needed only the single positive
dispatch-path proof `process-photos` needed, none of `check-approval`'s
negative-path investigation.

## Setup: `skills.external_dirs`

Unlike `process-photos`/`check-approval`, which landed in
`platform/photo-agent/skills` — already a watched directory by the time #8
shipped — this skill lands in a **new** parent directory,
`platform/email-agent/skills`, that the live `~/.hermes/config.yaml` does
not yet list:

```yaml
skills:
  external_dirs:
    - ~/src/fieldkit/platform/photo-agent/skills
    - ~/src/fieldkit/platform/email-agent/skills   # <- new, added by this issue
```

Confirmed by reading the live config directly (`~/.hermes/config.yaml` on
this machine) before writing this skill — only the photo-agent path was
present. Per #8's precedent, editing the live gateway config from an
automated session is out of scope here (it's running infrastructure outside
the repo, not a well-scoped action for this issue) — the new `external_dirs`
entry is instead a documented one-time setup step in `SETUP.md`, applied by
the admin along with `hermes gateway restart`, the same manual step #7/#8
already required for their own first install.

## Naming: hyphen vs. underscore

Named `check-email` (hyphenated) from the start — no rename step, same
posture #8 took for `check-approval`. Verified empirically against this
machine's real Hermes install (`~/.hermes/hermes-agent`), not assumed by
analogy to #7/#8's finding.

**Two different names are in play here, at two different layers — this is
the part worth being precise about, since an earlier draft of this doc
conflated them (flagged in #40's cross-review):**

- `/check-email` is Hermes's **internal canonical command key**
  (`agent.skill_commands`'s bookkeeping) — never typed by anyone, never
  seen by Telegram.
- `/check_email` (underscored) is the **actual registered Telegram bot
  command** — what shows up in the bot's command list and what the admin
  actually types. `hermes_cli.commands._sanitize_telegram_name()` converts
  the internal hyphenated key back to this underscored form at
  *registration* time, specifically because Telegram itself restricts bot
  command names to `[a-z0-9_]` (no hyphens allowed).

So the fact that `resolve_skill_command_key('check_email')` *also* resolves
to `/check-email` below is a separate, earlier step: it's Hermes's lookup
resolver treating hyphens/underscores as interchangeable when mapping any
typed form back to the internal key. It is not evidence of what the
registered Telegram command looks like — that's `_sanitize_telegram_name`'s
job, checked separately below. Both steps land on the same practical
outcome #7/#8 established (the admin's `/check_email` muscle memory is
unaffected), but they are two different functions solving two different
problems, not one.

Captured on this issue's worktree (`.worktrees/issue-25-check-email-skill`),
with `skills.external_dirs` in the probe's own patch pointed at that
worktree's `skills/` directory rather than the real `~/.hermes/config.yaml`
— the skill isn't merged to `main` yet, same technique #18's doc used for
`process-photos`:

```
$ ~/.hermes/hermes-agent/venv/bin/python -c "
from pathlib import Path
from unittest.mock import patch
from agent import skill_commands, skill_utils
from hermes_cli.commands import _sanitize_telegram_name

skills_dir = Path('~/src/fieldkit/.worktrees/issue-25-check-email-skill/platform/email-agent/skills').expanduser()
with patch.object(skill_utils, 'get_external_skills_dirs', return_value=[skills_dir]):
    commands = skill_commands.scan_skill_commands()
    print(commands['/check-email']['skill_md_path'])
    print(skill_commands.resolve_skill_command_key('check_email'))
    print(skill_commands.resolve_skill_command_key('check-email'))
print(_sanitize_telegram_name('check-email'))
"
/Users/sandeep_a_k/src/fieldkit/.worktrees/issue-25-check-email-skill/platform/email-agent/skills/check-email/SKILL.md
/check-email
/check-email
check_email
```

Reading this bottom-to-top against the two-layer explanation above: the
first three lines are the internal-key layer (`scan_skill_commands()` finds
this exact file and registers it as `/check-email`; both the underscored and
hyphenated *input* forms resolve back to that same internal key). The final
line, `check_email`, is `_sanitize_telegram_name`'s output — the layer that
actually determines the Telegram-registered command, confirming it comes out
underscored as expected. The internal-key resolution is reproduced
automatically by `test_check_email_dispatch.py`; the `_sanitize_telegram_name`
call is reproduced automatically by
`test_sanitize_telegram_name_converts_the_internal_key_to_the_registered_command`
in the same file (added after this
review round specifically to cover this boundary directly, rather than only
by inference from #7/#8's precedent).

## OpenClaw → Hermes mapping

Full reasoning lives as an HTML comment at the top of the skill file itself
(so it travels with the file, not just this doc). Summary:

- **Slash command**: no more `user-invocable: true` frontmatter field —
  every installed skill's `name` is automatically a slash command in Hermes.
  Named the skill `check-email` (hyphenated, agentskills.io-compliant from
  the start); the Telegram command the admin types stays `/check_email`.
- **Prerequisites**: OpenClaw's `metadata.openclaw.requires.bins: ["gws",
  "python3"]` had no Hermes equivalent — replaced with the
  agentskills.io-standard `prerequisites.commands` field (informational).
  Unlike `process-photos`, no explicit `which` checks were added to the
  body: `check_email.py` already fails loudly and reports a specific error
  for a missing/misconfigured `gws`, so there is nothing extra for the
  skill body to validate before dispatch — the same thin-body shape
  `check-approval` has.
- **No argument, no callback surface**: `check_email.py` takes no CLI
  arguments for a manual run (`--source` defaults to `"user"`, the correct
  default for this trigger). There is no argument-extraction or validation
  step in the skill body, unlike `process-photos`.
- **File location**: the old OpenClaw skill lived directly at
  `platform/email-agent/SKILL.md` (no `SKILL_<name>.md` prefix needed, since
  email-agent only ever hosted one skill, unlike photo-agent's
  `SKILL_process_photos.md` / `SKILL_check_approval.md` naming). That file
  is removed; the Hermes-native replacement lives at
  `platform/email-agent/skills/check-email/SKILL.md`, matching
  `process-photos`/`check-approval`'s `skills/{name}/SKILL.md` layout.
- **Everything else** (script invocation, verbatim-relay instructions,
  "do not improvise" instruction) is unchanged text from the OpenClaw
  skill's body.

## Verification

Same two-layer approach as #7/#8, plus this issue's own live empirical
check:

- `platform/email-agent/tests/test_check_email_skill.py` — structural
  consistency (frontmatter name, no leftover `openclaw`/`user-invocable`
  fields, prerequisites, verbatim-relay and no-improvisation instructions
  present, the old top-level `SKILL.md` is actually gone).
- `platform/email-agent/tests/test_check_email_dispatch.py` — real
  dispatch-path coverage, covering both layers described in "Naming" above
  separately rather than conflating them:
  - `scan_skill_commands()` / `resolve_skill_command_key()` (imported from
    the local Hermes install, executed via Hermes's own venv interpreter)
    against this skill's actual files, confirming Hermes discovers this
    exact file and registers it under the internal key `/check-email`, and
    that both `check_email` and `check-email` as *input* resolve back to
    that same internal key.
  - `hermes_cli.commands._sanitize_telegram_name('check-email') ==
    'check_email'` directly, confirming what the actually-registered
    Telegram bot command comes out as — the boundary the two functions
    above don't by themselves prove, added after #40's cross-review flagged
    the earlier draft of this doc for conflating the two.
  No LLM call — deterministic and fast. Skipped automatically where Hermes
  isn't installed.
- Manual verification (below).

- [x] Dispatch-resolution and Telegram-sanitization probes (above) run
  directly against this machine's real Hermes install and this skill's
  actual files — passed.
- [x] `test_check_email_dispatch.py` passes locally against the same
  install (14/14 tests green across both new test files).
- [ ] `hermes skills list --source local` against the *running* gateway —
  **not captured this pass**. Requires the `external_dirs` config addition
  documented above and a `hermes gateway restart`, which per #8's precedent
  is an admin action against live infrastructure, not something this issue
  performs from an automated session. Reasonable follow-up once merged and
  the admin applies the `SETUP.md` config step.
- [ ] Live Telegram `/check_email` round-trip — not done this pass, same
  gap #7/#8 left open pending live gateway access with the config change
  applied.

## Next steps

- Admin: add the `platform/email-agent/skills` entry to
  `~/.hermes/config.yaml`'s `skills.external_dirs` and run
  `hermes gateway restart` (see `SETUP.md`'s updated skill-install step)
- Live `hermes skills list` confirmation once the config change is applied
- Live Telegram round-trip for the manual `/check_email` command
- This closes out the three-skill Hermes migration started in #7 — no
  further manual-command ports are tracked as open issues as of this pass
