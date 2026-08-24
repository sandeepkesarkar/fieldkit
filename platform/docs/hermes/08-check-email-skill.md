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
analogy to #7/#8's finding:

```
$ ~/.hermes/hermes-agent/venv/bin/python -c "
from pathlib import Path
from unittest.mock import patch
from agent import skill_commands, skill_utils

skills_dir = Path('~/src/fieldkit/platform/email-agent/skills')
with patch.object(skill_utils, 'get_external_skills_dirs', return_value=[skills_dir]):
    commands = skill_commands.scan_skill_commands()
    print(commands['/check-email']['skill_md_path'])
    print(skill_commands.resolve_skill_command_key('check_email'))
    print(skill_commands.resolve_skill_command_key('check-email'))
"
/Users/.../platform/email-agent/skills/check-email/SKILL.md
/check-email
/check-email
```

Both the underscored and hyphenated forms resolve to the same
`/check-email` command key, scanned from this skill's actual `SKILL.md` —
reproduced automatically by `test_check_email_dispatch.py`. The
Telegram-facing command the admin types stays `/check_email` (Telegram's own
underscore-only restriction on bot command names, unaffected by the
frontmatter's hyphenated spelling).

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
  dispatch-path coverage. Runs Hermes's own `scan_skill_commands()` /
  `resolve_skill_command_key()` (imported from the local Hermes install,
  executed via Hermes's own venv interpreter) against this skill's actual
  files, confirming Hermes discovers this exact file, registers it under
  `/check-email`, and resolves both the Telegram-sanitized `/check_email`
  form and `/check-email` back to it. No LLM call — deterministic and fast.
  Skipped automatically where Hermes isn't installed.
- Manual verification (below).

- [x] Dispatch-resolution probe (above) run directly against this machine's
  real Hermes install and this skill's actual files — passed.
- [x] `test_check_email_dispatch.py` passes locally against the same
  install (13/13 tests green across both new test files).
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
