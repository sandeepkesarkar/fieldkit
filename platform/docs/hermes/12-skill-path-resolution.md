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

### Known limitation — and why it is not closed here

The substitution is textual, so the skill directory arrives as shell **source**,
not shell data. The quoted heredoc stops all expansion, but a heredoc still
terminates: a checkout path containing a newline followed by a line exactly
equal to the delimiter would end it early, and the remainder of the pathname
would run as shell code before any guard could inspect it.

**This cannot be fixed in bash.** Every bash quoting construct has a finite,
known terminator — `"` for double quotes, `'` for single quotes and `$'...'`,
a newline for a comment, a delimiter line for a heredoc — and a pathname may
contain any printable text, since the only bytes a path cannot hold are `/`
within a component and NUL, and no heredoc delimiter can contain NUL. For
every construct there therefore exists a pathname that escapes it. Round 2 of
the PR #75 review closed the double-quote hole; round 3 found the heredoc
delimiter hole; the pattern is structural, not a sequence of oversights.

### What a delimiter collision permits

**Everything.** Once the heredoc terminates early, the remainder of the
pathname is arbitrary shell source, executed with the gateway's privileges. It
is not confined to the injected fragment and it is not bounded by the guards
below: it can skip the guards, suppress the `ERROR` output, and exit 0,
so a caller sees an ordinary success. A pathname of

```
repo
<the delimiter>
cd ..
exit 0
#
```

produces exactly that on all four skills — verified through Hermes's real
`_build_skill_message()`. `test_delimiter_collision_permits_arbitrary_shell_source`
pins that behaviour so the limitation stays documented rather than drifting
back into an assurance.

No claim is made that the damage is contained. An earlier revision of this
document said the block "still refuses to `cd`" and "still reports `ERROR`" in
the residual case; that was true only of the one payload its test used, and
false for the class.

### Why the risk is accepted

The path comes from operator-configured `skills.external_dirs`. Creating a
pathname that collides with the delimiter requires someone who can already
write the Hermes config — i.e. who already has code execution as this user, and
therefore already has everything this would grant them. It is not a privilege
boundary FieldKit can defend, and it is not reachable by a Telegram sender or
by anything in the photo/email pipelines. The risk is accepted on that basis,
not on any claim about blast radius.

### What the mitigations actually buy

- The delimiter is long and improbable
  (`__FIELDKIT_SKILL_DIR_EOF_9c1f4b7e2a5d__`), so an accidental collision is
  extremely unlikely. This is protection against a coincidence, not against an
  adversary — an adversary reads the delimiter out of this file. What the
  regression test actually establishes is narrower than "unlikely": it fails
  if anyone shortens the delimiter back to a guessable string, which is the
  part that can be tested.
- A newline *without* a matching delimiter line truncates the value and fails
  closed, as do the other malformed inputs the suite exercises.

**The real fix is upstream.** If Hermes passed the skill directory to the
block as an environment variable (data) rather than pasting it into the skill
body (source), the whole class disappears and the guards here become
redundant. That is worth raising with Hermes rather than contorting four
`SKILL.md` files further.

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

## Where the record lives

The behaviour described in this document is exercised by
`platform/photo-agent/tests/test_skill_no_hardcoded_repo_paths.py`. Read that
file for what is actually demonstrated; it is the record, and it does not go
stale the way a summary does.

A previous revision carried a table mapping each statement here to the test
that pinned it. It was removed. In one review round it produced a miscount, a
pairing whose test did not cover the case it was cited for, an entry implying
more than its test showed, and an exception list that over-claimed in its own
right — then needed a second test to police the table itself. The tests are a
better record than a summary of the tests.

Two statements in this document are deliberately not backed by a test, and are
flagged here rather than left to read as though they were:

- **The structural argument** — that a quoting construct needs a terminator a
  pathname can contain, so pasting text into shell source cannot be made safe
  in bash. This is reasoning about bash, not a property of this code. It was
  demonstrated by escaping each candidate construct by hand and was verified
  as sound in review; it is not pinnable as a property of these files, which
  is why no test asserts it. The counterfactual examples in the `SKILL.md`
  comments (what a double-quoted or single-quoted interpolation would do) are
  in the same position: they *could* be pinned by direct bash demonstrations,
  and deliberately are not, because they describe constructs this code no
  longer uses and such tests would guard nothing that can regress here.
- **Threat model and environment** — that creating a colliding pathname needs
  write access to Hermes's config, and that `$FIELDKIT_ROOT` is absent from
  the live gateway's process environment. Facts about provenance and about one
  machine, established by inspection (`ps eww` on the gateway).

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

A guard aborts with one `ERROR:` line and exit 1. The table below lists the
messages as they are written, so there is no summary of them to drift out of
date. They are diagnostics **for the operator** — the agent's role is limited
to relaying them; it does not change configuration or restart the gateway.

| `ERROR:` line | Cause | Operator action |
|---|---|---|
| `skill directory unresolved — the HERMES_SKILL_DIR placeholder reached the shell unsubstituted` | `skills.template_vars` is off | Set `skills.template_vars: true`, restart the gateway |
| `refusing to run — the skill directory contains a shell metacharacter` | The checkout path contains `$`, a backtick, a quote or a backslash | Move the checkout to a path without those characters |
| `resolved directory is not a fieldkit platform agent directory` | The skill is not being read from a fieldkit checkout (e.g. copied into Hermes's own skills directory) | Point `skills.external_dirs` at the checkout instead of copying skills |
| `scripts/<name>.py not found under <dir>` | `external_dirs` points at an incomplete or wrong checkout | Correct the `external_dirs` path |
| `No such file or directory` on a `cd` | A skill has been re-hardcoded with an absolute path | Restore `${HERMES_SKILL_DIR}`; the regression test catches this in CI |
