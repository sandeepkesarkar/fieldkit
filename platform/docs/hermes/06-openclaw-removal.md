# OpenClaw Removal — Final Verification (Mac Mini / `servicehub-dev`)

Closes issue #14: uninstalling OpenClaw from the Mac Mini now that #13
(cron-script verification, [`05-cron-verification.md`](05-cron-verification.md))
and #24 (replacing the last live `openclaw message send` call sites with
direct Telegram Bot API calls) had cleared the two blockers #13's review
identified. Builds on [`01-install.md`](01-install.md) (#5),
[`02-gateway-setup.md`](02-gateway-setup.md) (#6), and
[`05-cron-verification.md`](05-cron-verification.md) (#13, #22, #23).

Source: [`platform/.specify/003-hermes-runtime/spec.md`](../../.specify/003-hermes-runtime/spec.md)
(FR-008, SC-001, SC-003).

**All of the removal, the crontab fix, and the security incident below were
performed live on the Mac Mini by the human, with agent guidance — not
reproduced or re-run by whichever agent is writing this doc.** This is a
record of what was already done and verified, not a new verification pass.

## Summary

1. OpenClaw's `launchd` gateway is stopped, durably disabled, and fully
   uninstalled from the Mac Mini — binary, global npm package, and config
   directory all gone, confirmed via follow-up checks.
2. A repo-wide grep for `openclaw` references, re-run and individually
   classified fresh for this doc, sorted every hit into four categories:
   five active docs that needed real content fixes (fixed in this PR — see
   §2), one orphaned unreferenced duplicate left as-is with justification
   (root `constitution.md`), two already-tracked items deliberately left
   out of this PR's scope (#25, #26), and the remaining historical
   `.specify/` spec-kit records and dated docs. Nothing live/active and
   undiscovered remains — see §2's "Acceptance criterion 3" note below for
   what that criterion is and isn't claiming.
3. A latent crontab regression in `check_email.py`'s entry (bare `python3`,
   same class of bug #22/#23 already fixed in the other two scripts' entries)
   was found and fixed during this same session.
4. A `TELEGRAM_BOT_TOKEN` propagation gap from PR #24 was found and fixed.
   Two distinct causes, not one: primarily, the real value was never
   backfilled into the live `.env` after #24 added the key to
   `.env.example` (this explains the warning repeating across every tick);
   secondarily, a one-cron-cycle-old read explains the single further
   warning logged immediately after the value was added. See §4.
5. A security incident during that investigation — a sub-agent briefly
   printed the live Telegram bot token into its own session transcript via a
   raw byte-dump of `.env` — was caught immediately, remediated (token
   rotated), and is tracked to closure in issue #27.
6. With 1–4 verified, SC-001 is satisfied for the OpenClaw-dependency-
   elimination and script-execution-health portions it covers — **but not
   in full**: `check_approval`'s Hermes-vs-cron `getUpdates` race (newly
   tracked in issue #29) and `check_email`'s manual-command status (#25,
   unverified) remain open. See "SC-001 — Final Status" below.

## 1. OpenClaw removed from the Mac Mini (2026-08-23)

Stopped and durably disabled the `launchd` service:

```
$ launchctl unload ~/Library/LaunchAgents/ai.openclaw.gateway.plist
$ launchctl disable gui/501/ai.openclaw.gateway
```

The `disable` step matters on its own: PR #16's review of the earlier
Hermes-cutover work (#6) flagged that an `unload` alone is session-scoped —
`launchd` will happily reload the job on next login or a `launchctl load`
without anyone intending it, because `unload` doesn't change the job's
enabled/disabled state, only whether it's currently running. That gap is
what #16 deferred to #14 to close. `launchctl disable` targets the
`gui/501/ai.openclaw.gateway` launchd domain/service label directly, not a
file — it records the disabled state in launchd's own database, independent
of whether the plist that originally defined the job still exists on disk.
Confirmed durable via:

```
$ launchctl print-disabled gui/501 | grep openclaw
"ai.openclaw.gateway" => disabled
```

Then removed the package and its data:

```
$ npm uninstall -g openclaw
removed 699 packages
$ rm -rf ~/.openclaw
```

And removed the plist that had defined the job. Confirmed nothing remains,
including the binary itself off `PATH`:

```
$ launchctl list | grep openclaw
(no output)
$ ls ~/.openclaw
ls: /Users/sandeep_a_k/.openclaw: No such file or directory
$ ls ~/Library/LaunchAgents/ | grep openclaw
(no output)
$ command -v openclaw
(no output, exit 1 — not found on PATH)
```

`ai.hermes.gateway` is unaffected by any of the above — it's a separate
`launchd` job and was not touched.

## 2. Repo-wide grep — complete inventory (re-run for this doc)

Issue #14's acceptance criteria explicitly scopes this to active code/config
— historical docs/specs mentioning OpenClaw for context are expected and
fine. Re-running the grep fresh (excluding `.git`, stale local worktrees,
and vendored `venv`/`node_modules` trees) for this doc, rather than trusting
an earlier pass:

```
$ grep -ril openclaw --include="*" . \
    | grep -vE '/\.git/|/\.worktrees/|/venv/|/\.venv/|/node_modules/'
```

A first pass of this doc classified the hits by directory/category and
hand-waved several individual files into "historical spec-kit records"
without actually reading each one. A cross-review of PR #28 caught that —
several of those files turned out to be **active, current-facing governing
docs** that still asserted OpenClaw/local-only-inference as present fact,
not historical record. Every hit is now individually read and classified
below; the ones that needed real content changes (not just
reclassification) were fixed in this PR.

**Fixed in this PR** — active docs that asserted OpenClaw or "no cloud AI
inference" as current fact, corrected to describe Hermes and the cloud
model-routing pivot (Anthropic default / OpenAI per-client, from #6/FR-004):

- `.specify/memory/constitution.md` — the framework-level constitution,
  authoritative for all client work. Its Architecture Constraints named
  OpenClaw as the runtime and stated "no cloud AI inference" as a hard
  constraint — both false since #6. This was already tracked as open issue
  **#9** with its own pre-approved acceptance criteria (`Runtime: OpenClaw`
  → `Runtime: Hermes Agent`, remove/rewrite the "no cloud AI inference" and
  Mac-Mini-hardware-transfer/data-locality claims retired by the Mac Mini →
  Cloud pivot). Implemented #9's criteria verbatim in this PR; version
  footer bumped to 1.1 with an amendment note.
- `clients/_template/.specify/constitution.md` — the template every new
  client is scaffolded from (per `CONTRIBUTING.md`'s onboarding steps).
  Same OpenClaw/no-cloud-inference claims, in the AI Provider header, the
  "OpenClaw Cost Model" and "OpenClaw Integration" sections, and the
  footer. Explicitly named in open issue **#10**'s acceptance criteria
  ("Check ... `clients/_template/.specify/constitution.md` for the same
  stale boilerplate; fix if found") — fixed here, though #10 also covers
  `spec-template.md` boilerplate that doesn't mention OpenClaw and wasn't
  touched by this PR's grep-driven fixes; #10 stays open for that part.
- `clients/_demo/.specify/constitution.md` — the live demo client's own
  constitution, marked `Status: Reference / Active`. Same class of claim
  (AI Provider header, cost model, integration section, footer), not
  covered by any existing issue. Fixed directly in this PR (admin decision,
  since it mirrors #9's already-approved pattern) — scope limited to the
  AI-provider/cloud-inference claims, plus one small follow-on: its two
  "all data stored locally on Mac Mini" bullets directly tied to the
  AI-inference Implications/Gate-1-equivalent sections got the same
  not-yet-re-confirmed-post-pivot caveat already used in the framework
  constitution's Gate 1 fix (reused wording, not new judgment). The
  separate Hardware Transfer Plan section and Data Storage/backup-ownership
  content in this file were deliberately left untouched — that's the
  larger, still-undecided cloud-pivot ownership question W3 is meant to
  resolve, not something this PR should unilaterally settle.
- `framework-philosophy.md` — root-level, unlinked from `README.md` but not
  historical/dated. Its "Self-Hosted by Default" principle claimed "powered
  by OpenClaw" and "no cloud dependency for AI inference." Fixed directly,
  same scope limitation as above (AI-provider/inference claims only).
- `.specify/templates/overrides/plan-template.md` — the override template
  used by every future `/speckit-plan` run. Its Phase 2 boilerplate output
  line named "updated cron/OpenClaw config." Simple rename to
  "cron/Hermes config" — not previously covered by #10 (which named
  `spec-template.md`, not `plan-template.md`).

**Orphaned duplicate, left as-is** — root-level `constitution.md`: byte-for-
byte identical (pre-fix) to `clients/_template/.specify/constitution.md`.
Not referenced by `README.md`, `CONTRIBUTING.md`, `CLAUDE.md`, or any
`.claude/skills/*` file — every reference to a client constitution in this
repo points at `clients/{name}/.specify/constitution.md`, never the bare
root path. This looks like a leftover duplicate from before the template
was properly separated out (untouched since the initial commit). Left
unedited here rather than guessed at — deleting or syncing it is a small
cleanup call for the repo owner, not something this docs PR should decide
unilaterally.

**Already-tracked, deliberately out-of-scope** — filed and left open on
purpose, not missed:
- Issue #25 — `platform/email-agent/SKILL.md` and `SETUP.md` still
  describe OpenClaw's skill-install flow; `email-agent`'s manual
  `/check_email` command was never ported to a Hermes skill (unlike
  `process-photos` and `check-approval`, #7/#8). `check_email.py`'s own
  logic no longer depends on the `openclaw` binary as of #24 — this is
  purely the manual-invocation doc/skill surface.
- Issue #26 — `README.md`, `clients/_demo/README.md`, and
  `clients/_template/README.md` still describe OpenClaw as the current AI
  runtime rather than Hermes.

**Historical records, read and confirmed genuinely historical** — each of
these documents a past decision or state as it was at the time, not a
current claim:
- Frozen `.specify/` spec-kit planning artifacts: `003-hermes-runtime/spec.md`
  (the Hermes migration spec itself — extensively discusses OpenClaw as the
  thing being replaced, by design), `002-photo-video-agent/*`,
  `001-email-agent/*`, `004-e2e-test-rig/*`.
- `platform/photo-agent/skills/check-approval/SKILL.md` and
  `process-photos/SKILL.md` — both carry an explicit "OpenClaw → Hermes
  mapping" section (#7/#8) documenting the port for future reference; the
  skills themselves are Hermes-only today.
- `platform/docs/cross-review.md`, and `platform/docs/hermes/01-install.md`
  through `05-cron-verification.md` — this doc's own predecessors, each a
  dated record of a specific PR's findings, already following this same
  convention (see the "no other files needed updating" note at the end of
  `05-cron-verification.md`).
- Test files (`test_process_photos.py`, `test_check_approval.py`,
  `test_check_email.py`, etc.) whose fixtures or docstrings reference the
  pre-#24 `openclaw message send` code path's history.
- Dated `updates/` LinkedIn posts and `CONTRIBUTING.md` (this PR's own new
  reference to issue #27's incident, in the debugging-hygiene note — a
  historical citation, not a current-runtime claim).

**Acceptance criterion 3 of #14, interpreted explicitly, not left implicit.**
Criterion 3 literally reads "Repo-wide grep confirms no remaining reference
to OpenClaw in active code/config." Taken completely literally, that's not
true today: `platform/email-agent/SETUP.md`/`SKILL.md` (#25) and the three
READMEs (#26) are still active references to OpenClaw as if it were current
— deliberately deferred, not fixed, exactly as this same investigation
already treated them when #13 closed (see that issue's acceptance criteria,
which carry the identical "historical docs/specs may still mention it for
context — that's fine and expected" carve-out this doc has been applying
throughout §2). The claim this PR actually makes is narrower and is the one
that matters for #14's real intent: **no new or previously-undiscovered
active OpenClaw reference remains unfiled and untracked.** Every active hit
this fresh grep found is already filed as #25 or #26, both open, both
deliberately left out of this PR's scope — pulling either into this PR
would turn a docs-only OpenClaw-removal closeout into a much larger,
unrelated docs-migration PR (rewriting `email-agent`'s Hermes skill surface
for #25, or the READMEs across three files for #26). Under that reading,
criterion 3 is met: the grep surfaced nothing new, and everything it did
surface already has an owner and a tracking issue. Under the fully literal
reading, it is not yet met, and won't be until #25 and #26 close.

## 3. Crontab regression — `check_email.py` had the same bare-`python3` bug

[`05-cron-verification.md`](05-cron-verification.md) (#22/#23) fixed
`check_approval.py` and `upload_facebook.py`'s crontab entries to use
`/usr/local/bin/python3` explicitly, after finding that cron's `PATH` puts
Homebrew's Python 3 first and it's missing `python-dotenv`/`requests`. At
the time, `check_email.py`'s entry was left as bare `python3` because its
imports were stdlib-only and it had no third-party dependency to be missing.

That stopped being true after #24: `check_email.py` now imports `requests`
(via `platform/email-agent/tools/telegram_api.py`, added in #24 to replace
the `openclaw message send` shell-out). Bare `python3` in the crontab entry
now resolves to Homebrew's Python 3.14.6, which doesn't have `requests`
installed — the identical failure mode #22/#23 already diagnosed and fixed
for the other two scripts, just latent until #24 gave `check_email.py` a
third-party import to trip over it.

Fixed the same way, on the live crontab:

```
*/5 * * * * env PATH=/opt/homebrew/bin:/usr/local/bin:/Users/sandeep_a_k/.nvm/versions/node/v24.15.0/bin:/usr/bin:/bin bash -c 'date && /usr/local/bin/python3 /Users/sandeep_a_k/src/fieldkit/platform/email-agent/scripts/check_email.py --source cron' >> /Users/sandeep_a_k/src/fieldkit/logs/cron.log 2>&1
```

(`check_email.py` runs on the 5-minute schedule, `*/5 * * * *`, not the
1-minute schedule `check_approval.py` and `upload_facebook.py` use — see
`05-cron-verification.md`'s "before/after" crontab listings. Schedule,
`PATH`, and log redirect unchanged from before this fix — only the bare
`python3` became the explicit `/usr/local/bin/python3` path.)

Confirmed via `tail -f logs/cron.log`, watched by a human across subsequent
ticks: no more `ModuleNotFoundError`, clean runs.

## 4. `TELEGRAM_BOT_TOKEN` propagation gap after #24

#24 replaced the `openclaw message send` shell-outs in `check_email.py`,
`check_approval.py`, `upload_facebook.py`, and `process_photos.py` with
direct Telegram Bot API calls through `telegram_api.py`, and added
`TELEGRAM_BOT_TOKEN=` to `platform/email-agent/.env.example`. It did not —
and had no way to, since real secrets don't belong in a PR diff — backfill
the actual value into the live `platform/email-agent/.env` on the Mac Mini.
Immediately after #24 landed, `check_email.py` logged on every cron tick:

```
WARNING __main__ _telegram: send failed — TELEGRAM_BOT_TOKEN is not set
```

**Two separate causes, not one — kept distinct here since conflating them
was a finding of PR #28's own cross-review.**

**Primary cause: the value was genuinely missing.** #24 added the
`TELEGRAM_BOT_TOKEN=` key to `platform/email-agent/.env.example` — correctly,
since a real secret value has no business in a PR diff — but that's a
template, not the live file. Nothing backfilled the actual value into
`platform/email-agent/.env` on the Mac Mini, so every cron tick read a
`.env` with no `TELEGRAM_BOT_TOKEN` key at all until someone did that by
hand. This accounts for the warning repeating across every tick from #24
landing until the manual `.env` edit below.

**Secondary, narrower cause: a one-cycle-old read explains only the single
warning logged immediately after the fix landed.** Before concluding the
fix had failed, the code itself was ruled out:

- `check_email.py`'s `_load_env()` parses `.env` generically with no
  key whitelist — nothing about `TELEGRAM_BOT_TOKEN` specifically that could
  be mishandled.
- Call ordering was correct — `_load_env()` runs before any `_telegram()`
  call.
- No cwd sensitivity — reproduced cron's faithful environment with `env -i`
  and confirmed the script resolves `.env` the same way regardless of
  invocation directory.

The one warning that appeared *after* the value was added was explained by
timing, not the code or a still-missing value: the `.env` edit landed
roughly 12 seconds after that tick had already started and read the
still-token-less file — a single stale snapshot from a tick already in
flight, not a recurrence of the primary cause. The very next tick, with the
edited file in place before the tick started, succeeded cleanly.

**Fixed** by adding the missing `TELEGRAM_BOT_TOKEN` value to
`platform/email-agent/.env` — copied from `~/.hermes/.env`, which already
held a working value for the same bot (reused from #6's setup, see
[`02-gateway-setup.md`](02-gateway-setup.md)). Verified clean across
subsequent cron ticks, watched by a human afterward.

## 5. Security incident and remediation — closes issue #27

During the investigation in §4, a sub-agent ran a raw byte-dump
(`tail -c 80 platform/email-agent/.env | xxd`) to inspect the file's raw
bytes and briefly printed the real `TELEGRAM_BOT_TOKEN` value in plaintext
into its own session transcript. This was caught immediately — not repeated
for the rest of the investigation, and not relayed into the orchestrator's
output or the human-facing conversation — but the plaintext value had
already been persisted to that sub-agent's session history, which is enough
to treat the token as compromised.

Filed and closed as **issue #27**, remediated:

1. Old token revoked and a new one issued via BotFather.
2. New value applied consistently to all three locations that share this
   token: `platform/email-agent/.env`, `clients/_demo/src/photo-agent/.env`,
   and `~/.hermes/.env`.
3. `ai.hermes.gateway` restarted (`launchctl kickstart -k gui/501/ai.hermes.gateway`)
   to pick up the new token.
4. Confirmed a clean reconnect with zero auth errors:
   `Telegram polling confirmed healthy` → `Connected to Telegram (polling
   mode)` → `✓ telegram connected`.

Full incident record and the closing remediation comment: issue #27.

One more gap found while writing this doc: the `.env` edits in §4's
remediation left a `platform/email-agent/.env.bak` backup file sitting
untracked in the working tree, and `.gitignore` didn't cover `.env.bak` —
only `.env` itself. That's the same class of exposure this incident was
about, just via `git add -A` instead of a transcript. Added `.env.bak` to
`.gitignore` in this PR to close that gap.

## 6. Debugging-hygiene note added

Issue #27's remaining acceptance item — a durable note instructing agents
never to raw-dump a file known to hold secrets — is added in this PR to
[`CONTRIBUTING.md`](../../../CONTRIBUTING.md)'s existing "Security & Secrets"
section, the most natural existing home for it in this repo. Summary: use
presence/length-only checks (`grep -c '^KEY='`, `awk '/^KEY=/{sub(/^[^=]*=/,"");
print length}'`) instead of `cat`/`xxd`/`tail -c`/`hexdump` on `.env` files or
credential stores.

## SC-001 — Final Status

The governing spec defines SC-001 broadly: *"Every existing chat-driven and
cron-driven flow (`check_email`, `process_photos`, `check_approval`,
`upload_facebook`) works under Hermes with no admin-visible behavior
change"* — not just "scripts execute without crashing." An earlier draft of
this section claimed SC-001 was fully satisfied by removing OpenClaw;
PR #28's cross-review correctly rejected that as overclaimed, since removing
OpenClaw doesn't touch the specific gap `05-cron-verification.md` already
identified. Corrected status, part by part:

**Satisfied by this doc: OpenClaw-dependency elimination and script-execution
health.**
- **OpenClaw dependency: zero.** #24 replaced every live `openclaw message
  send` call site with direct Telegram Bot API calls before this doc's
  removal step ran, and §1 confirms OpenClaw itself is no longer installed
  on the machine at all — there is no fallback path left to depend on it.
- **Cron scripts run clean with Hermes running:** confirmed by recorded
  human observation while investigating, not just asserted after the fact —
  §3 records a human watching `logs/cron.log` across ticks after the
  interpreter-path fix and seeing `check_email.py`'s `ModuleNotFoundError`
  stop appearing, and §4 records the same for the `TELEGRAM_BOT_TOKEN`
  warning after the `.env` fix, both while `ai.hermes.gateway` was up and
  polling. Neither this doc nor the PR retains a literal timestamped log
  excerpt from those sessions — the record here is the human's
  contemporaneous account of what `cron.log` showed, not a preserved raw
  transcript.
- **Repo-wide grep:** clean per §2, individually re-read and classified for
  this doc rather than citing an earlier pass.

**NOT satisfied — a real, unresolved gap, not fixed by removing OpenClaw:**
`check_approval.py`'s button-callback path. `05-cron-verification.md`
already documented this as a confirmed (not probabilistic) `getUpdates`
offset race between Hermes's continuous long-poll and the cron leg's
once-a-minute poll, both against the same bot token — Hermes wins
essentially every time, so a real button tap is very unlikely to reach the
cron leg. Removing OpenClaw does not fix this: it only removes a *third*
competitor for the same token: Hermes and the cron leg still both poll
`getUpdates` against it exactly as before. **Do not read this doc's
OpenClaw-removal work as resolving that race** — it doesn't touch it.

That gap's only prior tracking was inline commentary on issues #13 and #14,
both now closed — too fragile to rely on. Opened **issue #29** to track it
on its own, durable and currently open, referencing the original discovery
context (PR #21's review thread, 2026-08-21) and `05-cron-verification.md`'s
own "Acceptance Criteria — Final Status" item 4.

**Unverified, stated honestly rather than assumed either way:**
`check_email`'s manual `/check_email` chat command. Issue #25 (open) flags
that `email-agent`'s manual-command skill surface was never ported to
Hermes's format, and states it is "likely non-functional" — that's #25's
own hedge, not a confirmed result, and nothing in this PR verifies it either
way. `check_email.py`'s cron-triggered path is unaffected either way (its
own logic has zero OpenClaw dependency as of #24, per §1–§4 above) — the
open question is specifically whether the manual, chat-driven `/check_email`
invocation dispatches correctly under Hermes today. Left to #25 to verify
and close, consistent with that issue's own acceptance criteria.

**Net: SC-001 is satisfied for the OpenClaw-dependency-elimination and
script-execution-health portions this doc covers. It is not fully
satisfied** — the `check_approval` callback race (#29) and the
`check_email` manual-command question (#25) are both real, open gaps against
SC-001's full "no admin-visible behavior change" bar, and neither is
resolved by anything in this PR.
