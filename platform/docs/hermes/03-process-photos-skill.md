# process_photos as a Hermes Skill (issue #7)

Replaces OpenClaw's `SKILL_process_photos.md` with a Hermes-native skill at
`platform/photo-agent/skills/process_photos/SKILL.md`. `process_photos.py`
itself is unchanged — this is a dispatch-layer swap only, per FR-002.

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
the next turn. Verified with `hermes skills list`:

```
│ process_photos          │                      │ local   │ local   │ enabled │
```

Applied to the running gateway with `hermes gateway restart`.

## OpenClaw → Hermes mapping

Full reasoning lives as an HTML comment at the top of the skill file itself
(so it travels with the file, not just this doc). Summary:

- **Slash command**: no more `user-invocable: true` frontmatter field —
  every installed skill's `name` is automatically a slash command in Hermes.
  Named the skill `process_photos` (underscore, matching the script and the
  admin's existing `/process_photos` muscle memory) rather than following
  the hyphenated convention used by Hermes's own bundled skills.
- **Prerequisites**: OpenClaw's `metadata.openclaw.requires.bins` had no
  Hermes equivalent (no declarative prerequisite-enforcement mechanism).
  Used the agentskills.io-standard `prerequisites.commands` field
  (informational), and kept the actual enforcement as explicit `which`
  checks in the body — identical to what the OpenClaw skill already did.
- **Everything else** (argument extraction, validation regex, script
  invocation, verbatim-relay instructions) is unchanged text — these are
  LLM-followed prose instructions either way, not runtime-specific syntax.

## Verification

SKILL.md is prose, not executable code — `platform/photo-agent/tests/
test_process_photos_skill.py` checks structural consistency (frontmatter
name, prerequisites, the skill's stated validation regex matching
`scripts/process_photos.py`'s actual `_PROJECT_NAME_RE`, verbatim-relay
instructions present) but can't exercise real dispatch. Manual verification
instead:

- [x] `hermes skills list` shows `process_photos` discovered from the
  external dir, enabled
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
- #8 — rewrite `check_approval` the same way
