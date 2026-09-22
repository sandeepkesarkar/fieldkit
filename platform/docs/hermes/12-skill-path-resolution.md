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
below: it can skip every one of them, suppress all `ERROR` output, and exit 0,
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
  closed, as does every other malformed shape.

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

## Which test pins which statement

Five review rounds of this work found claims that outran their tests. This
table exists so that cannot recur quietly: every behavioural statement in this
document and in the four `SKILL.md` comments names the test that demonstrates
it. All live in
`platform/photo-agent/tests/test_skill_no_hardcoded_repo_paths.py`.

| Statement | Pinned by |
|---|---|
| No skill contains a hardcoded repo path | `test_no_hardcoded_repo_path` |
| Each skill resolves via the `${HERMES_SKILL_DIR}` token | `test_block_resolves_via_the_hermes_skill_dir_token` |
| Resolution lands on this checkout's agent directory | `test_resolution_lands_on_the_real_agent_dir` |
| Hermes really substitutes, and its output resolves | `test_real_hermes_builder_substitutes_and_resolves` |
| An unsubstituted placeholder fails closed | `test_unresolved_skill_dir_fails_closed` |
| The heredoc removes the expansion class (`$`, `$(...)`, backtick, quotes, backslash) | `test_shell_metacharacter_in_path_fails_closed` |
| A space in the path still works | `test_benign_path_shapes_still_resolve` |
| The delimiter is not a guessable string | `test_guessable_heredoc_delimiter_does_not_escape` |
| A newline alone fails closed | `test_newline_without_delimiter_line_fails_closed` |
| Delimiter text inside a component is harmless | `test_delimiter_text_inside_a_path_component_resolves` |
| A delimiter collision permits arbitrary shell source (bypass, no ERROR, exit 0) | `test_delimiter_collision_permits_arbitrary_shell_source` (both `model` and `hermes` mechanisms) |
| Each guard branch aborts with its own diagnostic, exit 1 | `test_each_guard_branch_aborts_with_its_own_diagnostic` |
| Every guard prints exactly one `ERROR:` line | `test_every_guard_branch_reports_one_error_line` |
| The two configuration branches name their Hermes setting | `test_configuration_branches_name_the_hermes_setting` |
| A path containing the text `HERMES_SKILL_DIR` is not mistaken for the placeholder | `test_path_containing_the_placeholder_name_is_not_mistaken_for_it` |
| Two-levels-up is enforced, not assumed | `test_each_guard_branch_aborts_with_its_own_diagnostic` (`outside-platform-parent`) |
| Current operator docs carry no stale repo path | `test_no_stale_repo_path_in_current_docs` |
| Dated records redirect to this document | `test_stale_config_snippets_point_at_the_current_location` |

### What is deliberately NOT test-pinned

Three kinds of statement above cannot be pinned by a unit test, and are marked
here so nobody mistakes them for tested guarantees:

- **Counterfactual rationale.** "In double quotes a `$(...)` path would
  execute; in single quotes an apostrophe would break out." These describe
  constructs the code no longer uses. They were demonstrated when they were
  live — the round-2 injection matrix ran 24 red against the double-quoted
  version — but no current test asserts them.
- **The structural argument.** "No bash construct prevents a delimiter
  collision, because each has a finite terminator a pathname may contain."
  This is a universal claim over all of bash, not a property of this code. It
  was demonstrated by escaping every candidate construct by hand, and
  independently verified as sound in review.
- **Threat model and environment.** "Creating such a path requires write
  access to Hermes's config"; "`$FIELDKIT_ROOT` is absent from the live
  gateway's process environment". These are facts about provenance and about
  one machine, established by inspection (`ps eww` on the gateway), not by the
  suite.

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

Every guard prints exactly one line beginning `ERROR:` and exits 1 —
`test_every_guard_branch_reports_one_error_line` pins that for each branch
below. Two of them name the Hermes setting at fault (`skills.template_vars`
and `skills.external_dirs`); the rest name the offending path instead, because
no setting is to blame for it. These are diagnostics **for the operator** —
the agent's role is limited to relaying them; it does not change configuration
or restart the gateway.

| `ERROR:` line | Cause | Operator action |
|---|---|---|
| `skill directory unresolved — the HERMES_SKILL_DIR placeholder reached the shell unsubstituted` | `skills.template_vars` is off | Set `skills.template_vars: true`, restart the gateway |
| `refusing to run — the skill directory contains a shell metacharacter` | The checkout path contains `$`, a backtick, a quote or a backslash | Move the checkout to a path without those characters |
| `resolved directory is not a fieldkit platform agent directory` | The skill is not being read from a fieldkit checkout (e.g. copied into Hermes's own skills directory) | Point `skills.external_dirs` at the checkout instead of copying skills |
| `scripts/<name>.py not found under <dir>` | `external_dirs` points at an incomplete or wrong checkout | Correct the `external_dirs` path |
| `No such file or directory` on a `cd` | A skill has been re-hardcoded with an absolute path | Restore `${HERMES_SKILL_DIR}`; the regression test catches this in CI |
