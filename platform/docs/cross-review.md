# Cross-Vendor Review for fieldkit PRs

Implements `dev-infrastructure/specs/github-task-workflow.md` FR-011 / SC-006
("Every code PR MUST carry a cross-vendor review... before being labeled
`needs-approval`") for fieldkit specifically. fieldkit issues aren't worked
through Polly/the poller (those are scoped to `dev-infrastructure` only, per
the Rollout Phase decision in `dev-infrastructure/specs/omnigent-setup.md`) —
so unlike `dev-infrastructure`, where Polly's `cross-review` skill dispatches
this automatically, here it's a step the implementer (so far, always me) runs
by hand before flipping a PR to `needs-approval`.

**Scope:** every fieldkit PR from issue #7 onward. (This doc's own PR, #17,
and the two PRs before it, #15 and #16, predate or introduce the mechanism —
see "Known gap" below for exactly what that means for each.)

**No linked issue?** Not every PR maps to a GitHub issue (this doc's own PR
is an example — process documentation, not part of an issue breakdown). When
there's no issue to pull an acceptance contract from, use the PR description
instead: its "Summary" and any explicitly stated goals stand in for the
`ACCEPTANCE CONTRACT` section in step 3 below.

## Why this mechanism specifically

- **Codex, not another vendor** — matches the pinned-reviewer decision made
  for `dev-infrastructure` (`omnigent/polly/skills/cross-review/SKILL.md`):
  Claude implements, a different vendor reviews.
- **`omnigent run --harness codex`, not the raw `codex` CLI directly** —
  Codex CLI silently prefers `OPENAI_API_KEY` (globally exported in
  `~/.zshrc`) over the ChatGPT subscription login whenever both are present,
  billing pay-per-token instead (the exact bug fixed for `dev-infrastructure`
  in `omnigent-setup.md`, "`codex-native` still billed the API key anyway").
  This isn't inferred from a billing record after the fact — it's how the
  harness is built: Omnigent's installed `codex` (non-native) harness
  excludes `OPENAI_API_KEY` from the subprocess environment by construction.
  In the installed package (`omnigent/inner/codex_executor.py`,
  `_CODEX_ENV_DENY_EXACT = frozenset({"OPENAI_API_KEY"})`, with the env
  filter applied in `_build_codex_env`), the source comment states the
  reason directly: *"`OPENAI_API_KEY` is stripped so the codex CLI falls
  back to subscription auth (`auth.json`) rather than a developer API key
  that would charge separately."* This matches `dev-infrastructure`'s own
  root-cause writeup for the same fix. A live test call from this directory
  (`omnigent run --harness codex --no-log -p "..."`) returned
  `total_cost_usd: None` on the session record, which is consistent with
  subscription billing — corroborating, not proving, the point on its own;
  the env-stripping behavior in the harness source is the actual guarantee.
- **Same review contract, not a duplicate one** — the actual checklist
  (Engineering + Security dimensions) lives in exactly one place,
  `dev-infrastructure/omnigent/polly/skills/cross-review/SKILL.md`'s
  "Standing review dimensions" section. Read it fresh at review time rather
  than copy-pasting it here, so fieldkit never reviews against a stale copy
  when that file gets extended with new dimensions later.

## Procedure

Run after a PR is open and its own tests/verification pass, before posting
the summary comment on the issue and flipping its label to `needs-approval`.

Steps 3 and 5 below pass untrusted content (a PR diff, an issue body, review
output) to shell commands. That content can contain backticks, `$(...)`,
`$VAR`, or quotes — if pasted directly into a double-quoted shell argument,
the invoking shell would interpret those before Codex or `gh` ever see them
(command injection in the documented procedure itself). Both steps route
untrusted content through a file instead of a shell string, for that reason.

1. Gather the diff and the issue's acceptance criteria (skip the `gh issue
   view` call if there's no linked issue — see "No linked issue?" above):
   ```bash
   gh pr diff <pr-number> --repo sandeepkesarkar/fieldkit
   gh issue view <issue-number> --repo sandeepkesarkar/fieldkit --json body
   ```

2. Read the current Standing Review Dimensions. The range below is
   deliberately trimmed with a second pass so the output doesn't include the
   trailing `## Notes` heading itself:
   ```bash
   sed -n '/## Standing review dimensions/,/^## Notes/p' \
     ~/src/dev-infrastructure/omnigent/polly/skills/cross-review/SKILL.md \
     | sed '$d'
   ```

3. Build the prompt in a temp file (never interpolate the diff/issue body
   directly into a shell string), then pass it to `omnigent run` via command
   substitution on the *whole file* — bash does not re-expand the content of
   a `$(...)` substitution, so this is safe even though the file itself
   contains untrusted text. A `trap` guarantees `.codex-tmp/` (Omnigent's
   per-session Codex home, which includes an `auth.json` copy per the
   `.gitignore` entry) is removed on both the success and failure path:

   ```bash
   cd ~/src/fieldkit
   trap 'rm -rf .codex-tmp' EXIT

   PROMPT_FILE=$(mktemp)
   cat > "$PROMPT_FILE" <<'PROMPT_EOF'
   Review this diff against the acceptance contract below. Do NOT edit any
   files -- report only.

   === ACCEPTANCE CONTRACT ===
   PROMPT_EOF
   # Issue body if there is one (see "No linked issue?"), else paste the PR
   # description instead -- either way, append via redirection, not `-p`:
   gh issue view <issue-number> --repo sandeepkesarkar/fieldkit --json body -q .body \
     >> "$PROMPT_FILE"

   printf '\n=== STANDING REVIEW DIMENSIONS (apply in addition to the contract above) ===\n' \
     >> "$PROMPT_FILE"
   sed -n '/## Standing review dimensions/,/^## Notes/p' \
     ~/src/dev-infrastructure/omnigent/polly/skills/cross-review/SKILL.md \
     | sed '$d' >> "$PROMPT_FILE"

   printf '\n=== DIFF ===\n' >> "$PROMPT_FILE"
   gh pr diff <pr-number> --repo sandeepkesarkar/fieldkit >> "$PROMPT_FILE"

   printf '\nReport blocking issues, non-blocking issues, and suggestions
   separately, grouped by dimension, each with file:line evidence. If a
   dimension finds nothing, say so explicitly.\n' >> "$PROMPT_FILE"

   omnigent run --harness codex --no-log -p "$(cat "$PROMPT_FILE")" \
     | tee /tmp/cross-review-output.txt

   rm -f "$PROMPT_FILE"
   # trap fires here on exit (success or failure), removing .codex-tmp/
   ```

   Immediately after `omnigent run --harness codex` creates `.codex-tmp/`
   (and before the `trap` cleanup fires), it holds a copy of `auth.json`.
   Restrict access for the life of that directory in case the trap doesn't
   get a chance to run (e.g. a killed shell):
   ```bash
   chmod 700 .codex-tmp 2>/dev/null
   chmod 600 .codex-tmp/auth.json 2>/dev/null
   ```
   Add this right after the `omnigent run` invocation starts producing
   `.codex-tmp/`, or fold it into a wrapper script if this procedure gets
   automated later.

4. Blocking findings → fix them, push the update, and re-run steps 1–3
   against the updated diff (mirrors Polly's own fix-loop in
   `cross-review/SKILL.md` step 5). Clean → proceed.

5. Post the review output as a PR comment straight from the file captured in
   step 3 (`-F`/`--body-file` reads the file directly — no shell
   interpolation of the untrusted review text at all), *then* post the
   one-line issue summary and flip the label to `needs-approval` — the
   review comment must already be on the PR before it reaches that state,
   per SC-006:
   ```bash
   gh pr comment <pr-number> --repo sandeepkesarkar/fieldkit \
     -F /tmp/cross-review-output.txt
   rm -f /tmp/cross-review-output.txt
   ```

## Known gap

Not every fieldkit PR before this doc went through this procedure, and
"predates the doc" doesn't uniformly mean "nothing outstanding" — the two
merged PRs before this one differ:

- **#15 / issue #5 (Install Hermes)** — merged before this mechanism
  existed. No known open findings against it; not worth re-opening to
  backfill a review comment with no decision left to inform.
- **#16 / issue #6 (Configure Hermes Telegram gateway)** — also merged
  before this mechanism existed, but an independent cross-vendor review run
  against it afterward surfaced four blocking findings that are **still
  unresolved**: OpenClaw's launchd gateway not durably disabled across
  reboot, broken/incomplete OpenAI switch-over instructions, a false claim
  about `OPENAI_API_KEY` being available, and unverified "Anthropic is
  default provider" evidence. These are not fixed, and merging #16 didn't
  resolve them. There is no single dedicated tracking issue for this
  finding set as of this writing — the closest related open issues are #14
  (OpenClaw uninstall, overlaps with the launchd-disable finding) and #12
  (OpenAI-backed demo customer, overlaps with the switch-over-instructions
  finding), but neither one names all four findings explicitly. Until a
  dedicated issue exists, treat #16's cross-review findings as open and
  unresolved — do not treat #16 as clean precedent.

Every fieldkit PR from issue #7 onward goes through this procedure.
