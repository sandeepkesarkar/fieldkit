# Hermes Agent — Cron-Script Verification (Mac Mini / `servicehub-dev`)

Covers issue #13: verifying the three cron-triggered scripts
(`check_email.py`, `check_approval.py`'s cron leg, `upload_facebook.py`)
remain untouched by the Hermes migration and still run via cron with
Hermes installed and running. Builds on
[`01-install.md`](01-install.md) (#5), [`02-gateway-setup.md`](02-gateway-setup.md) (#6),
and [`04-check-approval-skill.md`](04-check-approval-skill.md) (#8).

Source: [`platform/.specify/003-hermes-runtime/spec.md`](../../.specify/003-hermes-runtime/spec.md) (FR-003, SC-001).

> **Partially superseded by issue #49 (2026-08-26):** this doc's
> `check_approval.py`'s cron leg coverage describes a poller that issue
> #49 retires entirely — `check_approval.py` no longer accepts
> `--source cron` at all, and no crontab entry should run it on an interval
> going forward. See
> [`10-text-based-approval-migration.md`](10-text-based-approval-migration.md)
> for the retirement and the live-migration steps. `check_email.py`'s and
> `upload_facebook.py`'s cron entries are unaffected — only the
> photo-approval polling leg is retired.

> **Revision note (2026-08-22):** the version of this document written
> during PR #22 overstated several of its own conclusions — see issue #13
> (reopened) for the retroactive cross-vendor review that found this. This
> revision corrects the doc in place rather than appending an errata
> section, since an inaccurate "verification" doc is worse than none. The
> corrected verdicts are in "Acceptance Criteria — Final Status" below.

> **Addendum (issue #45):** the crontab lines shown throughout this doc
> (below, and the ones actually live on the Mac Mini as of this writing) all
> rely on the shared root `.env`'s `CLIENT_NAME` — the single-client-at-a-
> time posture these lines were written under. See "Running more than one
> client's cron entries concurrently" below for the per-entry override that
> makes two clients' cron-driven flows safe to run side by side, once
> that's actually needed. **This addendum is documentation only** — the
> live crontab itself has not been changed by it; migrating the live
> entries to per-client overrides is a separate, later step.

## Summary

The three scripts themselves are confirmed byte-for-byte unchanged since
before Feature 003 (Hermes migration) began. The **crontab entries that
invoke them were not** — they carried two live bugs, now fixed on the Mac
Mini, plus a live-data issue in one script's state file found during this
revision's re-verification (unrelated to the crontab or to Feature 003).

1. **Stale script paths** (pre-existing, from the Feature 002 photo-agent
   migration to `platform/`) — `check_approval.py` and `upload_facebook.py`'s
   crontab entries still pointed at the old `clients/_demo/src/photo-agent/scripts/`
   location. Every cron invocation failed at the OS level
   (`FileNotFoundError`) before Python ever started. **Fixed.**
2. **Wrong Python interpreter on `PATH`** (discovered while verifying the
   fix above) — cron's `PATH` lists `/opt/homebrew/bin` before
   `/usr/local/bin`, so the bare `python3` in the crontab entries resolved to
   Homebrew's Python 3.14, which has neither `python-dotenv` nor `requests`
   installed. This was completely masked by bug #1: the script was never
   found long enough to reach its imports. Once the path was fixed, both
   scripts immediately failed with `ModuleNotFoundError`. **Fixed** by
   pointing the two entries at `/usr/local/bin/python3` (Python 3.13, has
   both packages) explicitly — this matches the invocation already
   documented in `SKILL_upload_facebook.md`'s Cron Setup section.
3. **A stuck test record in `facebook_state.json` (live data, not a crontab
   or code bug)** — found 2026-08-22, during the re-verification prompted by
   #13 reopening. The `_demo` client's live
   `clients/_demo/data/photo-agent/facebook_state.json` had a
   `pending_facebook_upload` record left over from manual testing
   (`project_name="test_project"`, `idempotency_key="42"`,
   `video_local_path="/tmp/fieldkit_test/video.mp4"`, a path that does not
   exist on this machine) sitting at `status: "failed"`.
   `facebook_state.get_pending_upload()` returns whatever is in
   `pending_facebook_upload` unconditionally — it does not filter on
   `status` — so every cron tick re-picked up this dead record and logged a
   real (if handled) error, once a minute, indefinitely. This is what PR
   #22's evidence run actually captured (see "What changed on 2026-08-22"
   below) — it was not a clean success. **Fixed** by backing up the state
   file and clearing the stale record; see that section for the full
   before/after and the live cron.log confirmation.
4. **A test-isolation bug that recreated #3 on its own** — a photo-agent
   test fixture wasn't mocking the `facebook_state` calls it needed to,
   so simply running `pytest` on this machine wrote a real record straight
   into the same live file. Found by a second cross-review, fixed in the
   test suite itself, and verified with a before/after diff of the live
   file. See "Test-suite isolation bug" below — this is the one genuinely
   new code fix in this revision; everything else here is docs plus
   one-time live-data cleanup.

`check_email.py`'s crontab entry was never stale and is unaffected by
either crontab bug (its imports are stdlib-only; it never depends on
`dotenv` or `requests`). No change was made to its entry.

## FR-003 — scripts unchanged; OpenClaw dependency corrected

Diffed all three scripts against `1283c9d^` (the commit immediately before
"Add spec: Hermes runtime migration (Platform Feature 003)"), and again
against current `main` tip (`6346412`, which includes the merged
check_approval Hermes-skill work from #8/#21):

```
$ git diff 1283c9d^ -- platform/photo-agent/scripts/check_approval.py \
                        platform/photo-agent/scripts/upload_facebook.py \
                        platform/email-agent/scripts/check_email.py
(empty)
```

All three files are byte-for-byte identical to their pre-Feature-003 state.
No `hermes` import, env var, or runtime dependency was added to any of the
three scripts' invocation paths — that part of FR-003 holds.

**The "zero OpenClaw dependency" framing does not hold, and PR #22's
original wording of this section was wrong to imply it did.** The
`openclaw` references in `check_approval.py` and `check_email.py` are not
vestigial — they are real, live, currently-reachable code paths:

- `check_approval.py:76` — `_openclaw_send()` shells out to
  `openclaw message send --channel telegram -m <message>` to deliver the
  approve/reject confirmation and error-alert messages (called at lines
  369, 381, 387, 408).
- `check_email.py:145` — the same `openclaw message send` CLI call, used
  for its own Telegram notification.

Both predate Feature 003 (they're part of the original Feature 001/002
builds, carried over unchanged — the `git diff` above confirms that part).
But "predates Feature 003" and "zero dependency today" are different
claims, and only the first is true. **This is a real, live dependency
#14's OpenClaw-uninstall work needs to explicitly account for**: uninstalling
OpenClaw without first replacing or reworking these two call sites will
break `check_approval.py`'s and `check_email.py`'s Telegram notification
paths, not just the button-callback race documented below.

## Crontab — before and after (literal invocations)

Live crontab backed up before each edit (not tracked in git — this repo
does not track a copy of the crontab anywhere; searched for one, none
exists — see "Known gap" below). Backups on the Mac Mini:
`/tmp/fieldkit-crontab-backup.txt` (pre-path-fix) and
`/tmp/fieldkit-crontab-backup2-1787426110.txt` (pre-interpreter-fix, post-path-fix).

These are the literal crontab lines, not paraphrased — `crontab -l` output
at each stage:

**1. Before (broken — stale path, from `fieldkit-crontab-backup.txt`):**
```cron
*/5 * * * * env PATH=/opt/homebrew/bin:/usr/local/bin:/Users/sandeep_a_k/.nvm/versions/node/v24.15.0/bin:/usr/bin:/bin bash -c 'date && python3 /Users/sandeep_a_k/src/fieldkit/platform/email-agent/scripts/check_email.py --source cron' >> /Users/sandeep_a_k/src/fieldkit/logs/cron.log 2>&1
* * * * * env PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin bash -c 'python3 /Users/sandeep_a_k/src/fieldkit/clients/_demo/src/photo-agent/scripts/check_approval.py --source cron' >> /Users/sandeep_a_k/src/fieldkit/logs/cron.log 2>&1
* * * * * env PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin bash -c 'python3 /Users/sandeep_a_k/src/fieldkit/clients/_demo/src/photo-agent/scripts/upload_facebook.py --source cron' >> /Users/sandeep_a_k/src/fieldkit/logs/cron.log 2>&1
```

**2. Intermediate (path fixed, interpreter still bare `python3`, from
`fieldkit-crontab-backup2-1787426110.txt`):**
```cron
*/5 * * * * env PATH=/opt/homebrew/bin:/usr/local/bin:/Users/sandeep_a_k/.nvm/versions/node/v24.15.0/bin:/usr/bin:/bin bash -c 'date && python3 /Users/sandeep_a_k/src/fieldkit/platform/email-agent/scripts/check_email.py --source cron' >> /Users/sandeep_a_k/src/fieldkit/logs/cron.log 2>&1
* * * * * env PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin bash -c 'python3 /Users/sandeep_a_k/src/fieldkit/platform/photo-agent/scripts/check_approval.py --source cron' >> /Users/sandeep_a_k/src/fieldkit/logs/cron.log 2>&1
* * * * * env PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin bash -c 'python3 /Users/sandeep_a_k/src/fieldkit/platform/photo-agent/scripts/upload_facebook.py --source cron' >> /Users/sandeep_a_k/src/fieldkit/logs/cron.log 2>&1
```

**3. Current (path + interpreter both fixed — live now, confirmed via
`crontab -l` on 2026-08-22):**
```cron
*/5 * * * * env PATH=/opt/homebrew/bin:/usr/local/bin:/Users/sandeep_a_k/.nvm/versions/node/v24.15.0/bin:/usr/bin:/bin bash -c 'date && python3 /Users/sandeep_a_k/src/fieldkit/platform/email-agent/scripts/check_email.py --source cron' >> /Users/sandeep_a_k/src/fieldkit/logs/cron.log 2>&1
* * * * * env PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin bash -c '/usr/local/bin/python3 /Users/sandeep_a_k/src/fieldkit/platform/photo-agent/scripts/check_approval.py --source cron' >> /Users/sandeep_a_k/src/fieldkit/logs/cron.log 2>&1
* * * * * env PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin bash -c '/usr/local/bin/python3 /Users/sandeep_a_k/src/fieldkit/platform/photo-agent/scripts/upload_facebook.py --source cron' >> /Users/sandeep_a_k/src/fieldkit/logs/cron.log 2>&1
```

`check_email.py`'s line never changed across all three states. Schedule,
`PATH` env var, and log redirect were left untouched in both edits to the
other two lines — only the script path and (in the second edit) the
interpreter path changed.

## Running more than one client's cron entries concurrently (issue #45)

The crontab entries above (and the ones actually live on the Mac Mini) all
depend on the shared root `fieldkit/.env`'s `CLIENT_NAME` — every cron tick
of `upload_facebook.py` (and, before issue #49 retired its cron leg,
`check_approval.py`) runs against whatever client that one shared file
currently names. That's fine for a single client at a time, but it means
two clients' cron-driven flows cannot correctly coexist: there is no way
for one cron entry to say "run this against `venus`" while another says
"run this against `mercury`" — both would read the same `CLIENT_NAME`.

**Mechanism (supported by the scripts themselves — a documentation-and-
crontab-authoring convention, not a new runtime mechanism):**
`process_photos.py`, `check_approval.py`, `upload_facebook.py`, and
`run_e2e_test.py` (plus the e2e stage scripts and `generate_auth_link.py`)
each load env vars in two steps — `load_dotenv(_ROOT / ".env",
override=False)` (the shared root file) followed by
`load_dotenv(.../clients/<client>/.../.env", override=True)` (that client's
own secrets, which are allowed to override), then re-assert
`os.environ["CLIENT_NAME"]` immediately after that second load in case a
client `.env` ever defined its own conflicting `CLIENT_NAME`. `override=False`
is pinned explicitly on the first call — this repo owns that contract
rather than leaning on `python-dotenv`'s current default, which
`requirements.txt` does not pin a version for — so it never clobbers a
`CLIENT_NAME` already present in the process's environment when the script
starts. That means a `CLIENT_NAME` set inline on the invocation itself
always wins over the root `.env`'s value — verified empirically (see
`platform/photo-agent/tests/test_client_name_override.py`), not assumed
from reading the `python-dotenv` docs alone.

Adopting this convention: give each client its own crontab line, with
`CLIENT_NAME=` set inline on that line, instead of one shared line per
script that relies on the root `.env`:

```cron
* * * * * env PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin CLIENT_NAME=venus bash -c '/usr/local/bin/python3 /Users/sandeep_a_k/src/fieldkit/platform/photo-agent/scripts/upload_facebook.py --source cron' >> /Users/sandeep_a_k/src/fieldkit/logs/cron.log 2>&1
* * * * * env PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin CLIENT_NAME=mercury bash -c '/usr/local/bin/python3 /Users/sandeep_a_k/src/fieldkit/platform/photo-agent/scripts/upload_facebook.py --source cron' >> /Users/sandeep_a_k/src/fieldkit/logs/cron.log 2>&1
```

`env CLIENT_NAME=<client>` sets the variable for that one crontab-spawned
process only — it never writes to the shared root `.env`, so there is no
mutable state one client's cron entry (or a manual/e2e test invocation
using the same inline-override pattern) could accidentally repoint at
another client's credentials or data. A crontab entry with no inline
`CLIENT_NAME=` keeps resolving from the shared root `.env` exactly as
before, so today's single-client-at-a-time posture (the only thing live on
this Mac Mini right now) is completely unaffected by this convention
existing.

This addendum is documentation and test coverage only. **The live crontab
on this Mac Mini has not been changed** — it still runs the single shared-
`.env` form shown in "Current" above, because only one client
(`_demo`) is live today. Adopting the per-entry override form above (and
adding entries for additional clients) is a live-migration step for
whenever a second client's cron-driven flow actually needs to run
alongside `_demo`'s — not part of this change.

**Known gap:** there is no tracked crontab source-of-truth in this repo, and
no automated drift check between what's documented here and what's
actually live on the Mac Mini — this doc is a point-in-time snapshot, and it
already drifted from reality once (this is exactly how the stale-path bug
went unnoticed). A follow-up idea, not built here: commit a `crontab.example`
or similar file and add a periodic check (e.g. a cron entry or a `#7`-style
skill) that diffs live `crontab -l` output against it and alerts on drift.

## Evidence — each script's actual cron invocation

**`check_email.py`** — never stale, unaffected by either crontab bug. Runs
in `logs/cron.log` complete cleanly with no error; earlier runs in the same
log show successful Telegram delivery (`✅ Sent via Telegram. Message ID:
87/90`).

**`check_approval.py`** — ran the exact cron invocation manually after the
interpreter fix:
```
$ bash -c '/usr/local/bin/python3 /Users/sandeep_a_k/src/fieldkit/platform/photo-agent/scripts/check_approval.py --source cron'
(no output)
$ echo $?
0
```
Silent exit 0 is the documented behavior when there is no pending approval
in `state.json` — script-level success at the OS/import level: no crash, no
stale-path or missing-module error. This still says nothing about the
button-callback path — see "Acceptance Criteria — Final Status" below.

**`upload_facebook.py`** — this is where PR #22's original evidence was
wrong. The run captured in that PR was:
```
$ bash -c '/usr/local/bin/python3 /Users/sandeep_a_k/src/fieldkit/platform/photo-agent/scripts/upload_facebook.py --source cron'
ERROR:__main__:video file missing: project=test_project path=/tmp/fieldkit_test/video.mp4
ERROR:tools.facebook_state:mark_failed: key=42
$ echo $?
0
```
PR #22 characterized this as "the script's own error-handling path
completing normally, not a crash — confirms script-level cron success."
That framing is misleading: exit 0 there means "an error was caught and
logged," not "there was nothing to do." Per the summary above, this was a
stuck test record that cron re-picked up on literally every tick.

**Corrected chronology** (an earlier version of this section claimed the
error repeated "for at least the four days between 2026-08-21 and this
revision," which does not match the log — that date range is about a day,
not four, and the cited sample only covered ~23 minutes). What `logs/cron.log`
actually shows, checked directly:
- **2026-06-20, continuous, all day** (00:00–23:55, every 5-minute tick):
  the identical two-line error, from an earlier stuck test record — a
  separate incident from the one this revision cleaned up, not portrayed as
  connected to it.
- **2026-08-22, ~15:20–15:38** (once cron's interpreter fix let the script
  actually run far enough to reach this code path — see PR #22's
  interpreter-bug fix above): the identical error, once a minute, until
  this revision's first cleanup at 15:38.
- **2026-08-22, ~15:50–15:57**: the same record reappeared with a fresh
  `triggered_at` timestamp — recreated by an unrelated bug (running the
  photo-agent test suite in an environment with a live `FB_PAGE_ID` wrote
  a real test record through to this same file; see "Test-suite isolation
  bug" below) — and was cleared again after that bug was fixed.

Calling any of this "success" evidence was the bug this revision exists to
correct.

## Test-suite isolation bug — tests were writing through to live production data

A second cross-review (of this revision's own first draft) found that
running `pytest platform/photo-agent/tests/` recreated the exact stuck
record above, with a `triggered_at` timestamp matching the test run. Root
cause: `test_check_approval.py`'s shared `base` fixture mocked the
approval-state calls but not `facebook_state.set_pending_upload` /
`is_published`. Every approve-path test using `base` calls into
`check_approval.py`'s `_enqueue_facebook_upload()`, which is gated only on
`FB_PAGE_ID` being set in the process environment — and it was, because
this Mac Mini's real client `.env` has a real `FB_PAGE_ID` (the Facebook
upload feature is live), and `check_approval.py` loads that `.env` at
import time regardless of which test is running. Nothing in the shared
fixture stopped these tests from reaching the real, unmocked
`facebook_state` module and writing to whatever `FIELDKIT_DATA_DIR`
happened to resolve to — the live `_demo` client's data directory, in the
normal case of running tests on this machine with its normal `.env` in
place.

**Fixed** in `platform/photo-agent/tests/test_check_approval.py`:
`facebook_state.set_pending_upload` and `.is_published` are now mocked
unconditionally in the shared `base` fixture (previously only in the
narrower `base_fb` fixture used by a handful of FB-enqueue-specific
tests), and `FB_PAGE_ID` is explicitly cleared by default in the `env`
fixture `base` builds on — so every test gets both protections regardless
of which fixture it uses, and neither an ambient `FB_PAGE_ID` nor a future
test that forgets to mock this path can write through again.

**Verified, not just claimed:** re-ran the full suite with
`FIELDKIT_ROOT` pointed at this machine's real fieldkit checkout (so the
real client `.env` — real `FB_PAGE_ID`, real `FIELDKIT_DATA_DIR` — was
genuinely in play, reproducing the exact condition that caused the
corruption) and diffed the live `facebook_state.json`'s MD5 before and
after:
```
$ md5 -q clients/_demo/data/photo-agent/facebook_state.json
d2aeb5429baa94f52d8e971d101b3b3c
$ FIELDKIT_ROOT=/Users/sandeep_a_k/src/fieldkit python3 -m pytest tests/ -q
395 passed in 1.76s
$ md5 -q clients/_demo/data/photo-agent/facebook_state.json
d2aeb5429baa94f52d8e971d101b3b3c
```
Identical hash — the live file was not touched. The record the test suite
had recreated was then cleared again (same backup-verify-clear procedure
as below), and confirmed to stay clear across live cron ticks afterward.

## What changed on 2026-08-22 (this revision) — full timeline

1. Backed up the live state file:
   `cp -p clients/_demo/data/photo-agent/facebook_state.json
   /tmp/fieldkit-facebook_state-backup-20260822153839.json`.
2. Verified the stuck record's fields matched the exact stale artifact
   described in the reopened issue (`project_name="test_project"`,
   `idempotency_key="42"`, `video_local_path="/tmp/fieldkit_test/video.mp4"`,
   `status="failed"`) before touching anything, and confirmed no other
   pending/real job existed in that file.
3. Cleared `pending_facebook_upload` to `null`, leaving
   `published_idempotency_keys` untouched.
4. Watched live `logs/cron.log` across multiple real cron ticks
   post-cleanup (15:41:26–15:43:31, spanning the `check_approval.py` and
   `upload_facebook.py` per-minute schedule) — **zero new lines appended**.
   This confirms the error itself is gone, not just suppressed.
5. Ran the exact cron invocation manually for a genuine clean run:
   ```
   $ bash -c '/usr/local/bin/python3 /Users/sandeep_a_k/src/fieldkit/platform/photo-agent/scripts/upload_facebook.py --source cron'
   $ echo $?
   0
   ```
   No output, no error — this is the actual "nothing to do, ran clean" case
   PR #22 should have shown and didn't.
6. This record came back once, on its own, about 12 minutes later
   (cleanup at 15:38:39; the recreated record's `triggered_at` was
   15:50:16 EDT) — recreated by the test-suite isolation bug documented
   above, with a fresh `triggered_at` timestamp from the test run.
   Backed up again
   (`/tmp/fieldkit-facebook_state-backup-20260822155712.json`), re-verified
   the same four fields, and cleared it a second time — this time after
   fixing the test isolation bug, and confirmed via the MD5 diff above that
   a full test-suite run no longer touches this file. Watched two more full
   minutes of live `cron.log` (15:57:24–15:59:30) afterward: zero new
   lines, and the file itself confirmed unchanged.

**Minor future hardening item (non-blocking):** the backup path used here
(`/tmp/fieldkit-facebook_state-backup-<timestamp>.json`, matching the
pattern already used for the crontab backups) is predictable and world-
readable on a shared `/tmp`. Fine for a single-admin Mac Mini today; worth
moving to a permissions-restricted backup location if this machine is ever
shared or this becomes a routine operation.

## Acceptance Criteria — Final Status

Issue #13's original four criteria, with corrected verdicts (the original
closing PR #22 marked all four as satisfied; that was wrong for three of
them):

1. **"Crontab entries unchanged from the pre-migration state."** ❌ as
   literally worded — **corrected criterion: unchanged except for the 2
   corrective path/interpreter repairs documented here.** The entries did
   drift pre-migration (stale path, then the masked interpreter bug); both
   were genuine bugs, not migration-introduced changes, and both are now
   fixed. But "unchanged" is not an accurate description of what happened,
   and the doc should not have implied it was.
2. **"Each script runs successfully via its existing cron invocation with
   Hermes installed and running."** Partially met. `check_email.py` and
   `upload_facebook.py` (post cleanup) now run cleanly with no error, and
   this is confirmed evidence, not a masked one. `check_approval.py`'s cron
   leg runs cleanly at the script level, but see #4 below — its actual
   *purpose* (handling the button-tap callback) is not met while Hermes is
   running.
3. **"No import, env var, or runtime dependency on Hermes or OpenClaw
   introduced into these three scripts."** ✅ for Hermes — confirmed by the
   `git diff` above. ❌ as worded for OpenClaw: there IS a live OpenClaw
   dependency in two of the three scripts (see "FR-003" above). It is not
   *introduced* by this migration, but the literal criterion ("no runtime
   dependency on OpenClaw") is not satisfied by the current state of the
   code, and shouldn't be marked as passed.
4. **`check_approval.py`'s button-callback role, under SC-001.** ⚠️ **Fix
   implemented in code (issue #29); pending live verification, see
   [`07-callback-race-fix.md`](07-callback-race-fix.md).** There is a known,
   previously-confirmed (not probabilistic — deterministic given
   `python-telegram-bot`'s `Updater._start_polling` behavior)
   offset-consumption race: Hermes's continuous `getUpdates` long-poll on
   the shared bot token would consume and advance past a real button-tap
   callback before the cron leg's once-a-minute poll saw it, essentially
   every time. That was not a theoretical edge case that might occur under
   contention — it was the default outcome of running Hermes's gateway and
   the cron leg simultaneously against the same token. Fixing this was
   explicitly out of scope for #13/#14 (both closed without touching it;
   tracked forward as #29) and is **not** fixed by OpenClaw's removal (see
   `06-openclaw-removal.md`'s SC-001 section) — only by giving the
   button-callback surface its own dedicated bot token, implemented in #29's
   PR. **Not yet marked resolved:** the second bot has not yet been
   registered via BotFather, the live `.env` on the Mac Mini has not been
   updated with `TELEGRAM_APPROVAL_BOT_TOKEN`, and no human has confirmed a
   real button tap while both Hermes and the cron leg are running
   simultaneously — `07-callback-race-fix.md`'s "Setup step" and
   "Verification" sections spell out exactly what's still outstanding. This
   item should be updated to ✅ only after that live check actually happens.
   Full source-level detail: [`04-check-approval-skill.md`](04-check-approval-skill.md)
   ("Known follow-up risk"), `spec.md` FR-002a, and `07-callback-race-fix.md`
   for the fix itself, the options considered, and what's automatically
   tested vs. what still needs a live human tap to confirm.

Neither OpenClaw nor `launchd` was touched during this or the original
verification session. `launchctl list` still shows both gateways loaded:
```
$ launchctl list | grep -E "openclaw|hermes"
470  0  ai.hermes.gateway
456  0  ai.openclaw.gateway
```

## Live docs already correct, no other files needed updating

`SKILL_upload_facebook.md`'s Cron Setup section already documented the
correct current path and the explicit `/usr/local/bin/python3` interpreter
— it was the crontab that had drifted from it, not the docs. The stale
`clients/_demo/src/photo-agent/scripts/` path still appears in several
`.specify/` planning artifacts (`002-photo-video-agent/plan.md`,
`004-e2e-test-rig/*.md`, `platform/.specify/002-photo-agent/*.md`) — these
are frozen historical spec-kit records of decisions made at the time and
were left untouched, consistent with how other completed features' specs
are treated.
