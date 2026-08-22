# Hermes Agent — Cron-Script Verification (Mac Mini / `servicehub-dev`)

Covers issue #13: verifying the three cron-triggered scripts
(`check_email.py`, `check_approval.py`'s cron leg, `upload_facebook.py`)
remain untouched by the Hermes migration and still run successfully with
Hermes installed and running. Builds on
[`01-install.md`](01-install.md) (#5), [`02-gateway-setup.md`](02-gateway-setup.md) (#6),
and [`04-check-approval-skill.md`](04-check-approval-skill.md) (#8).

Source: [`platform/.specify/003-hermes-runtime/spec.md`](../../.specify/003-hermes-runtime/spec.md) (FR-003, SC-001).

## Summary

The three scripts themselves are confirmed byte-for-byte unchanged since
before Feature 003 (Hermes migration) began. The **crontab entries that
invoke them were not** — they carried two live bugs, both now fixed on the
Mac Mini. Neither bug was introduced by this issue's work; both predate it
and were previously invisible because the first bug masked the second.

1. **Stale script paths** (pre-existing, from the Feature 002 photo-agent
   migration to `platform/`) — `check_approval.py` and `upload_facebook.py`'s
   crontab entries still pointed at the old `clients/_demo/src/photo-agent/scripts/`
   location. Every cron invocation failed at the OS level
   (`FileNotFoundError`) before Python ever started. **Fixed.**
2. **Wrong Python interpreter on `PATH`** (newly discovered while verifying
   the fix above) — cron's `PATH` lists `/opt/homebrew/bin` before
   `/usr/local/bin`, so the bare `python3` in the crontab entries resolved to
   Homebrew's Python 3.14, which has neither `python-dotenv` nor `requests`
   installed. This was completely masked by bug #1: the script was never
   found long enough to reach its imports. Once the path was fixed, both
   scripts immediately failed with `ModuleNotFoundError`. **Fixed** by
   pointing the two entries at `/usr/local/bin/python3` (Python 3.13, has
   both packages) explicitly — this matches the invocation already
   documented in `SKILL_upload_facebook.md`'s Cron Setup section, which uses
   an explicit interpreter path for exactly this reason.

`check_email.py`'s crontab entry was never stale and is unaffected by either
bug (its imports are stdlib-only; it never depends on `dotenv` or
`requests`). No change was made to its entry.

## FR-003 — scripts unchanged, no Hermes/OpenClaw dependency introduced

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
The `openclaw` references visible in `check_approval.py` and
`check_email.py` (e.g. `_openclaw_send()`, the `openclaw message send` CLI
call) predate Feature 003 entirely — they were part of the original
Feature 001/002 builds, not introduced by this migration. No `hermes` or
`openclaw` import, env var, or runtime dependency was added to any of the
three scripts' invocation paths.

## Crontab — before and after

Live crontab backed up before each edit (not tracked in git — this repo
does not track a copy of the crontab anywhere; searched for one, none
exists). Backups: `/tmp/fieldkit-crontab-backup.txt` (pre-path-fix) and
`/tmp/fieldkit-crontab-backup2-*.txt` (pre-interpreter-fix), on the Mac Mini.

**Before (broken — stale path):**
```cron
*/5 * * * * env PATH=... bash -c 'date && python3 .../platform/email-agent/scripts/check_email.py --source cron' >> .../logs/cron.log 2>&1
* * * * * env PATH=... bash -c 'python3 .../clients/_demo/src/photo-agent/scripts/check_approval.py --source cron' >> .../logs/cron.log 2>&1
* * * * * env PATH=... bash -c 'python3 .../clients/_demo/src/photo-agent/scripts/upload_facebook.py --source cron' >> .../logs/cron.log 2>&1
```

**After (fixed — correct path + explicit interpreter):**
```cron
*/5 * * * * env PATH=... bash -c 'date && python3 .../platform/email-agent/scripts/check_email.py --source cron' >> .../logs/cron.log 2>&1
* * * * * env PATH=... bash -c '/usr/local/bin/python3 .../platform/photo-agent/scripts/check_approval.py --source cron' >> .../logs/cron.log 2>&1
* * * * * env PATH=... bash -c '/usr/local/bin/python3 .../platform/photo-agent/scripts/upload_facebook.py --source cron' >> .../logs/cron.log 2>&1
```

Schedule, `PATH` env var, and log redirect were left untouched in both
edits — only the script path and (in the second edit) the interpreter path
changed.

## Evidence — each script's actual cron invocation

**`check_email.py`** — never stale, unaffected by either bug. Most recent
runs before this session's edits (`logs/cron.log`, 2026-08-22 15:15:01)
completed cleanly with no error; earlier runs in the same log show
successful Telegram delivery (`✅ Sent via Telegram. Message ID: 87/90`).

**`check_approval.py`** — ran the exact cron invocation manually after the
interpreter fix:
```
$ bash -c '/usr/local/bin/python3 .../platform/photo-agent/scripts/check_approval.py --source cron'
(no output)
$ echo $?
0
```
Silent exit 0 is the documented behavior when there is no pending approval
in `state.json` — script-level success, no crash, no stale-path or
missing-module error. Confirmed via live `cron.log` output at 15:16–15:18
after the fix: no further tracebacks from either photo-agent script.

**`upload_facebook.py`** — ran the exact cron invocation manually:
```
$ bash -c '/usr/local/bin/python3 .../platform/photo-agent/scripts/upload_facebook.py --source cron'
ERROR:__main__:video file missing: project=test_project path=/tmp/fieldkit_test/video.mp4
ERROR:tools.facebook_state:mark_failed: key=42
$ echo $?
0
```
Exit 0 with a handled application-level error (a stale test job already
present in `facebook_state.json` from prior testing — unrelated to this
issue, not introduced by this session). This is the script's own
error-handling path completing normally, not a crash — confirms
script-level cron success.

## What is explicitly NOT verified — `check_approval.py`'s button-callback race

This issue's acceptance criterion ("each script runs successfully via its
existing cron invocation with Hermes installed and running") is verified at
the **script level** for all three scripts, per the evidence above.

It is **not** verified end-to-end for `check_approval.py`'s button-tap
approval flow, and should not be assumed to work from the above. There is a
known, previously-confirmed (not probabilistic) race between Hermes's
gateway and the cron leg's `getUpdates` polling over the shared Telegram bot
token: `python-telegram-bot`'s `Updater._start_polling` advances the shared
per-token offset for every update it receives regardless of whether its own
handler acts on it. Hermes polls continuously; the cron leg polls once a
minute. Hermes will consume and offset-past a real button-tap callback
before the cron leg's next run almost every time. Full detail and source
citations: [`04-check-approval-skill.md`](04-check-approval-skill.md)
("Known follow-up risk") and `spec.md` FR-002a.

This was not fixed or worked around here — that's explicitly #14's scope
(uninstall OpenClaw / resolve the shared-token arrangement), not this
issue's. `launchctl list` still shows both gateways loaded during this
verification:
```
$ launchctl list | grep -E "openclaw|hermes"
470  0  ai.hermes.gateway
456  0  ai.openclaw.gateway
```
Neither OpenClaw nor `launchd` was touched in this session.

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
