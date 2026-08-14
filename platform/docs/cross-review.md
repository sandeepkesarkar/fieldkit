# Cross-Vendor Review for fieldkit PRs

Implements `dev-infrastructure/specs/github-task-workflow.md` FR-011 / SC-006
("Every code PR MUST carry a cross-vendor review... before being labeled
`needs-approval`") for fieldkit specifically. fieldkit issues aren't worked
through Polly/the poller (those are scoped to `dev-infrastructure` only, per
the Rollout Phase decision in `dev-infrastructure/specs/omnigent-setup.md`) —
so unlike `dev-infrastructure`, where Polly's `cross-review` skill dispatches
this automatically, here it's a step the implementer (so far, always me) runs
by hand before flipping a PR to `needs-approval`.

## Why this mechanism specifically

- **Codex, not another vendor** — matches the pinned-reviewer decision made
  for `dev-infrastructure` (`omnigent/polly/skills/cross-review/SKILL.md`):
  Claude implements, a different vendor reviews.
- **`omnigent run --harness codex`, not the raw `codex` CLI directly** —
  Codex CLI silently prefers `OPENAI_API_KEY` (globally exported in
  `~/.zshrc`) over the ChatGPT subscription login whenever both are present,
  billing pay-per-token instead (the exact bug fixed for `dev-infrastructure`
  in `omnigent-setup.md`, "`codex-native` still billed the API key anyway").
  Omnigent's non-native `codex` harness strips that env var before launch —
  confirmed working from this directory: `total_cost_usd: None` on the
  session record (no token-priced billing recorded) after a live test call.
- **Same review contract, not a duplicate one** — the actual checklist
  (Engineering + Security dimensions) lives in exactly one place,
  `dev-infrastructure/omnigent/polly/skills/cross-review/SKILL.md`'s
  "Standing review dimensions" section. Read it fresh at review time rather
  than copy-pasting it here, so fieldkit never reviews against a stale copy
  when that file gets extended with new dimensions later.

## Procedure

Run after a PR is open and its own tests/verification pass, before posting
the summary comment on the issue and flipping its label to `needs-approval`.

1. Gather the diff and the issue's acceptance criteria:
   ```bash
   gh pr diff <pr-number> --repo sandeepkesarkar/fieldkit
   gh issue view <issue-number> --repo sandeepkesarkar/fieldkit --json body
   ```

2. Read the current Standing Review Dimensions:
   ```bash
   sed -n '/## Standing review dimensions/,/^## Notes/p' \
     ~/src/dev-infrastructure/omnigent/polly/skills/cross-review/SKILL.md
   ```

3. Dispatch Codex, headless, from the fieldkit repo root (so its shell tool
   calls land in the right working directory if it wants to inspect files
   beyond the diff):
   ```bash
   cd ~/src/fieldkit
   omnigent run --harness codex --no-log -p "Review this diff against the acceptance
   contract below. Do NOT edit any files -- report only.

   === ACCEPTANCE CONTRACT (issue #<n>) ===
   <issue body>

   === STANDING REVIEW DIMENSIONS (apply in addition to the contract above) ===
   <pasted section from step 2>

   === DIFF ===
   <pasted diff from step 1>

   Report blocking issues, non-blocking issues, and suggestions separately,
   grouped by dimension, each with file:line evidence. If a dimension finds
   nothing, say so explicitly."
   ```

4. Blocking findings → fix them, push the update, and re-run step 3 against
   the updated diff (mirrors Polly's own fix-loop in `cross-review/SKILL.md`
   step 5). Clean → proceed.

5. Post the review output as a PR comment (`gh pr comment <pr-number> --body
   "..."`), *then* post the one-line issue summary and flip the label to
   `needs-approval` — the review comment must already be on the PR before it
   reaches that state, per SC-006.

## Known gap

fieldkit PRs merged before this doc existed (#15 / issue #5, #16 / issue #6)
went out without this step — done directly, before the mechanism existed.
Not retroactively reviewed; not worth re-opening merged, working PRs to
backfill a comment with no decision left to inform. Every fieldkit PR from
issue #7 onward goes through this procedure.
