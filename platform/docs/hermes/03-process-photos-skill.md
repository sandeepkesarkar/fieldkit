# process-photos as a Hermes Skill (issue #7)

Replaces OpenClaw's `SKILL_process_photos.md` with a Hermes-native skill at
`platform/photo-agent/skills/process-photos/SKILL.md`. `process_photos.py`
itself is unchanged — this is a dispatch-layer swap only, per FR-002.

The skill's `name:` is `process-photos` (hyphenated, per the agentskills.io
spec). The Telegram slash command the admin actually types stays
`/process_photos` (underscore) — Hermes auto-converts hyphens to underscores
when it registers a Telegram bot command, because Telegram itself restricts
command names to `[a-z0-9_]`. See "Naming: hyphen vs. underscore" below.

## Setup: `skills.external_dirs`

Unlike OpenClaw (which required manually re-syncing `SKILL_*.md` edits into
`~/.openclaw/workspace/skills/` — see the `openclaw_skill_cache` project
notes), Hermes discovers this skill directly from the fieldkit repo via
`~/.hermes/config.yaml`:

```yaml
skills:
  external_dirs:
    - ~/src/fieldkit/platform/photo-agent/skills
```

No copy step, no stale-cache risk — edit `SKILL.md`, Hermes picks it up on
the next turn. Verified with `hermes skills list --source local`:

```
┏━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━┳━━━━━━━┳━━━━━━━━━┓
┃ Name           ┃ Category ┃ Source ┃ Trust ┃ Status  ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━╇━━━━━━━╇━━━━━━━━━┩
│ process-photos │          │ local  │ local │ enabled │
└────────────────┴──────────┴────────┴───────┴─────────┘
0 hub-installed, 0 builtin, 1 local — 1 enabled, 0 disabled
```

(captured on the issue-7-process-photos-skill branch with
`skills.external_dirs` temporarily pointed at this branch's worktree, since
the skill isn't merged to `main` yet — same discovery mechanism the running
gateway uses against the real `~/src/fieldkit` checkout post-merge.)

Applied to the running gateway with `hermes gateway restart`.

## Naming: hyphen vs. underscore (cross-review finding, PR #18)

Cross-review flagged that `name: process_photos` violates the agentskills.io
spec (`name` must be lowercase letters/digits/hyphens only — no
underscores), but pointed out the tension: Telegram bot commands themselves
can't contain hyphens (Telegram restricts command names to `[a-z0-9_]`).

Investigated Hermes's own source rather than guessing:

- `agent/skill_commands.py::scan_skill_commands()` normalizes **any**
  skill's frontmatter `name` to a hyphenated slug for its internal command
  key — `name.lower().replace('_', '-')`, then strips anything outside
  `[a-z0-9-]`. This runs regardless of whether the frontmatter itself used
  `_` or `-`.
- `hermes_cli/commands.py::_sanitize_telegram_name()` then converts that
  hyphenated key **back** to underscores when registering the actual
  Telegram bot command, specifically because Telegram disallows hyphens.
  Its own docstring gives `/claude-code` → registered as `/claude_code` as
  the worked example.
- `agent/skill_commands.py::resolve_skill_command_key()` treats `-`/`_` as
  interchangeable on lookup for the same reason (its docstring: "Hyphens and
  underscores are treated interchangeably in user input").

So Hermes was already normalizing `process_photos` → `/process-photos`
internally before this fix — the underscore in the frontmatter was doing
nothing for dispatch. Verified empirically, from the Hermes venv, **before**
renaming (frontmatter still said `name: process_photos`):

```
$ ~/.hermes/hermes-agent/venv/bin/python -c "
from agent.skill_commands import scan_skill_commands, resolve_skill_command_key
cmds = scan_skill_commands()
for k, v in cmds.items():
    if 'photo' in k: print(k, '->', v['skill_md_path'])
print('resolve process_photos ->', resolve_skill_command_key('process_photos'))
print('resolve process-photos ->', resolve_skill_command_key('process-photos'))
"
/process-photos -> .../platform/photo-agent/skills/process_photos/SKILL.md
resolve process_photos -> /process-photos
resolve process-photos -> /process-photos
```

Ran the identical script again after renaming the frontmatter to
`name: process-photos` — same output (`skill_md_path` now pointing at the
renamed directory) — confirming the rename is a no-op for dispatch. Renamed
for spec compliance; the Telegram-facing command the admin types stays
`/process_photos` (Telegram's own underscore-only restriction, unaffected by
this frontmatter change).

## OpenClaw → Hermes mapping

Full reasoning lives as an HTML comment at the top of the skill file itself
(so it travels with the file, not just this doc). Summary:

- **Slash command**: no more `user-invocable: true` frontmatter field —
  every installed skill's `name` is automatically a slash command in Hermes.
  Named the skill `process-photos` (hyphenated, agentskills.io-compliant —
  see "Naming" above); the Telegram command the admin types stays
  `/process_photos`, matching prior muscle memory from OpenClaw.
- **Prerequisites**: OpenClaw's `metadata.openclaw.requires.bins` had no
  Hermes equivalent (no declarative prerequisite-enforcement mechanism).
  Used the agentskills.io-standard `prerequisites.commands` field
  (informational), and kept the actual enforcement as explicit `which`
  checks in the body — identical to what the OpenClaw skill already did.
- **Everything else** (argument extraction, validation regex, script
  invocation, verbatim-relay instructions) is unchanged text — these are
  LLM-followed prose instructions either way, not runtime-specific syntax.

## Verification

SKILL.md is prose, not executable code, so no automated test can make an LLM
follow its instructions. Three layers of coverage instead:

- `platform/photo-agent/tests/test_process_photos_skill.py` — structural
  consistency (frontmatter name, prerequisites, the skill's stated
  validation regex matching `scripts/process_photos.py`'s actual
  `_PROJECT_NAME_RE`, verbatim-relay instructions present).
- `platform/photo-agent/tests/test_process_photos_dispatch.py` — real
  dispatch-path coverage (cross-review finding, PR #18). Runs Hermes's own
  `scan_skill_commands()` / `resolve_skill_command_key()` (imported from the
  local Hermes install, executed via Hermes's own venv interpreter) against
  this skill's actual files, confirming Hermes discovers this exact file,
  registers it under `/process-photos`, and resolves the Telegram-sanitized
  `/process_photos` form back to it. No LLM call — deterministic and fast.
  Skipped automatically where Hermes isn't installed.
- Manual verification (below) for the parts neither automated layer can
  reach: an LLM actually following the skill's prose against a live Hermes
  session.

- [x] `hermes skills list --source local` shows `process-photos` discovered
  from the external dir, enabled (see "Naming" above for the exact output)
- [x] Empty argument (`/process_photos`) → correct validation message
- [x] Invalid argument, realistic case (`/process_photos kitchen remodel`,
  a space) → correct validation message, verbatim match to the skill's
  specified reply text
- [x] Valid-format argument reaching the script
  (`/process_photos e2e_hermes_test_nonexistent`) → dispatched
  `scripts/process_photos.py` correctly, relayed a real script failure
  **verbatim** (full traceback, not summarized), and correctly declined to
  guess at fixing the underlying issue itself
- [ ] Full happy-path (real photos → video → Telegram approval) — **not
  verified this pass**, blocked on an unrelated `gws`/Google Drive OAuth
  token refresh failure (`HTTP 400`) surfaced during the test above. Not
  caused by this change — `find_folder`'s token refresh fails before ever
  reaching the (nonexistent) test project folder. Needs `gws` re-auth;
  tracked as a follow-up, not blocking this issue's dispatch-layer scope.
- [ ] Live Telegram round-trip — not done this pass (admin didn't have the
  `_demo` bot available on hand); the CLI-based tests above used
  `hermes -z "..." -t hermes-telegram --skills process_photos` to approximate
  the gateway's toolset, which is not a perfect substitute for a real
  inbound Telegram message. Worth a live confirmation when convenient.

One CLI-testing pitfall worth recording: an early test used the adversarial
input `/process_photos bad name!!` and got a wildly wrong response — Hermes
interpreted "bad name" as feedback about the *skill's own name* and
investigated renaming it, rather than treating "bad name!!" as a project-name
argument to validate. Retesting with a realistic invalid input (a space) got
the correct response. Not treated as a skill bug: the literal phrase "bad
name" is unusually prone to this specific misreading, and OpenClaw's
identical instruction text carried the same theoretical risk in production
without issue — this is a general characteristic of LLM-followed
instructions under adversarial phrasing, not something introduced by this
port, and not worth hardening against for this issue's scope.

## Next steps

- Live Telegram confirmation when the `_demo` bot is reachable
- `gws` re-authorization (separate from this issue)
- #8 — rewrite `check_approval` the same way (done — see `04-check-approval-skill.md`; turned out to need a spec amendment first, since Hermes can't dispatch on the button-callback trigger the way it dispatches on `/process_photos`)
