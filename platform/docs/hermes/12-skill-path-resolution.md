# 12 — Skill path resolution and live deployment checks

**This is the current location for the skill-install config and the live
verification steps.** The numbered docs 01–11 are dated records of what was
done at the time; where they quote a repo path or a `skills.external_dirs`
entry, that value is as-of-then and may be stale. Use this document.

Introduced by issue #74 / PR #75.

---

## How a skill finds the repo

Each dispatched skill derives the repo location from **its own directory**. It
never contains a repo path.

Hermes substitutes the `${HERMES_SKILL_DIR}` token in `SKILL.md` with the
absolute skill directory before the agent sees the content
(`agent/skill_preprocessing.py::substitute_template_vars`, reached from
`build_skill_invocation_message()` on the Telegram slash-command path). It is
gated on `skills.template_vars`, which defaults to `true`. The agent directory
is two levels up:

```
<repo>/platform/<agent>/skills/<skill>/SKILL.md   <- ${HERMES_SKILL_DIR}
<repo>/platform/<agent>/                          <- two levels up, where the script runs
```

Consequence: moving or renaming the checkout requires **no edit to any skill**.
Hermes reads `external_dirs` in place with no copy step, so the skill always
resolves the checkout it is actually being read from.

Two properties are deliberate and enforced by tests
(`platform/photo-agent/tests/test_skill_no_hardcoded_repo_paths.py`):

1. **The substituted path is read as data, not as shell source.** The
   substitution is textual and happens before Bash parses the block, so
   interpolating the token in double quotes would let a path containing `$`,
   `$(...)` or a backtick expand or execute; single quotes would break on a
   path containing a single quote. The block reads the path through a quoted
   heredoc (`<<'DELIM'`), which expands nothing.
2. **Fail closed.** Any condition that leaves the repo location uncertain
   aborts with an `ERROR:` line instead of running somewhere else. Issue #59
   was a cross-client data leak caused by a silent fallback.

### Layout assumption

Two-levels-up is correct for skills living in a fieldkit checkout, which is how
they are installed here. It is *not* universally valid: Hermes also supports
skills kept under its own skills directory, where two levels up is the Hermes
profile, not an agent directory. Rather than leave that as a documented
assumption, each block enforces it — the resolved directory must sit under a
`platform/` parent and must contain the script the skill dispatches to.
Installing a fieldkit skill outside a checkout therefore fails closed with a
named error rather than running against an unrelated directory.

---

## Deployment prerequisite — required, not optional

`skills.external_dirs` must list **both** agents' skill directories. The live
config lists only `platform/photo-agent/skills`, so `/check_email` is not
registered as a command at all — `scan_skill_commands()` returns the three
photo skills and no `check-email` entry. Merging PR #75 does not change that:
the path fix and the registration gap are independent, and `/check_email`
stays unavailable until this is applied.

Run in a normal login shell, so it targets the default profile
(`hermes config` writes to the profile named by `HERMES_HOME`, which is unset
in a login shell and therefore the default):

```bash
# 1. Confirm which profile you are about to change, and what is currently set
hermes gateway list
hermes config get skills.external_dirs

# 2. Set both entries (absolute paths; Hermes reads them in place)
hermes config set skills.external_dirs \
  '["/Users/sandeep_a_k/src/project-fieldkit-dev/fieldkit/platform/photo-agent/skills","/Users/sandeep_a_k/src/project-fieldkit-dev/fieldkit/platform/email-agent/skills"]'

# 3. Read it back before restarting
hermes config get skills.external_dirs
```

If the repo ever moves again, this config is the **only** place that needs
updating — the skills themselves follow automatically.

---

## Restart and confirm registration

```bash
hermes gateway restart
hermes gateway status

# All four commands must be listed
hermes skills list | grep -E 'process-photos|photo-approve|photo-reject|check-email'
```

`hermes gateway restart` is also all that is needed after any `SKILL.md`
change — there is no copy step and no stale cache.

---

## Live verification — the four Telegram commands

Send each in Telegram and check the response shape. The point is to confirm the
command **dispatched and the script ran**, not merely that the bot replied.

| Command | Expected on a healthy install |
|---|---|
| `/photo_reject` | `No pending approval.` (exit 0, empty stdout) — **not** a directory error |
| `/photo_approve` | `No pending approval.`, or the approval confirmation if something is pending |
| `/process_photos` | The prompt `Please provide a project name — e.g. /process_photos kitchen_remodel` (validated before any path resolution) |
| `/process_photos zzz_nonexistent` | A script-level Drive error naming the project — proves `process_photos.py` actually executed |
| `/check_email` | The inbox-cycle output, or `No new emails.` |

Watch the gateway log while testing:

```bash
tail -f ~/.hermes/logs/gateway.log
```

---

## Failure modes and what they mean

Every failure prints one `ERROR:` line, names the Hermes setting involved, and
stops. These are diagnostics **for the operator** — the agent's role is limited
to relaying them; it does not change configuration or restart the gateway.

| `ERROR:` line | Cause | Operator action |
|---|---|---|
| `skill directory unresolved — the HERMES_SKILL_DIR placeholder reached the shell unsubstituted` | `skills.template_vars` is off | Set `skills.template_vars: true`, restart the gateway |
| `refusing to run — the skill directory contains a shell metacharacter` | The checkout path contains `$`, a backtick, a quote or a backslash | Move the checkout to a path without those characters |
| `resolved directory is not a fieldkit platform agent directory` | The skill is not being read from a fieldkit checkout (e.g. copied into Hermes's own skills directory) | Point `skills.external_dirs` at the checkout instead of copying skills |
| `scripts/<name>.py not found under <dir>` | `external_dirs` points at an incomplete or wrong checkout | Correct the `external_dirs` path |
| `No such file or directory` on a `cd` | A skill has been re-hardcoded with an absolute path | Restore `${HERMES_SKILL_DIR}`; the regression test catches this in CI |
