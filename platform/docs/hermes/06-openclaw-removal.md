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
2. A repo-wide grep for `openclaw` references, re-run fresh for this doc,
   confirms the only remaining hits are the two already-tracked,
   deliberately out-of-scope items (#25, #26) plus expected historical
   `.specify/` spec-kit records — nothing live or active.
3. A latent crontab regression in `check_email.py`'s entry (bare `python3`,
   same class of bug #22/#23 already fixed in the other two scripts' entries)
   was found and fixed during this same session.
4. A `TELEGRAM_BOT_TOKEN` propagation gap from PR #24 was found, root-caused
   as a one-cron-cycle timing issue rather than a code bug, and fixed.
5. A security incident during that investigation — a sub-agent briefly
   printed the live Telegram bot token into its own session transcript via a
   raw byte-dump of `.env` — was caught immediately, remediated (token
   rotated), and is tracked to closure in issue #27.
6. With 1–4 verified, **SC-001 (cron scripts run correctly with Hermes
   running, with zero OpenClaw dependency) is now genuinely satisfied** —
   see "SC-001 — Final Status" below.

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
what #16 deferred to #14 to close. Confirmed durable via:

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

And removed the plist itself (the file `launchctl disable` was pointed at).
Confirmed nothing remains:

```
$ launchctl list | grep openclaw
(no output)
$ ls ~/.openclaw
ls: /Users/sandeep_a_k/.openclaw: No such file or directory
$ ls ~/Library/LaunchAgents/ | grep openclaw
(no output)
```

`ai.hermes.gateway` is unaffected by any of the above — it's a separate
`launchd` job and was not touched.

## 2. Repo-wide grep — confirmed clean (re-run for this doc)

Issue #14's acceptance criteria explicitly scopes this to active code/config
— historical docs/specs mentioning OpenClaw for context are expected and
fine. Re-running the grep fresh (excluding `.git`, stale local worktrees,
and vendored `venv`/`node_modules` trees) for this doc, rather than trusting
the earlier pass:

```
$ grep -ril openclaw --include="*" . \
    | grep -vE '/\.git/|/\.worktrees/|/venv/|/\.venv/|/node_modules/'
```

Every hit falls into one of two buckets:

- **Already-tracked, deliberately out-of-scope** — filed and left open on
  purpose, not missed:
  - Issue #25 — `platform/email-agent/SKILL.md` and `SETUP.md` still
    describe OpenClaw's skill-install flow; `email-agent`'s manual
    `/check_email` command was never ported to a Hermes skill (unlike
    `process-photos` and `check-approval`, #7/#8). `check_email.py`'s own
    logic no longer depends on the `openclaw` binary as of #24 — this is
    purely the manual-invocation doc/skill surface.
  - Issue #26 — `README.md`, `clients/_demo/README.md`, and
    `clients/_template/README.md` still describe OpenClaw as the current AI
    runtime rather than Hermes. `_template`'s copy matters most, since it's
    what every future client is scaffolded from.
- **Historical spec-kit records** — frozen `.specify/` planning artifacts
  (e.g. `003-hermes-runtime/spec.md`, `002-photo-video-agent/*`,
  `001-email-agent/*`, `004-e2e-test-rig/*`), test files exercising the
  pre-#24 code paths' history, and dated `updates/` posts. These describe
  decisions and states as they were at the time and are intentionally left
  untouched, consistent with how other completed features' specs are
  treated (see the equivalent note at the end of
  [`05-cron-verification.md`](05-cron-verification.md)).

No hit represents a live, reachable OpenClaw dependency. Acceptance
criterion 3 of #14 ("Repo-wide grep confirms no remaining reference to
OpenClaw in active code/config") is met.

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
* * * * * env PATH=/opt/homebrew/bin:/usr/local/bin:/Users/sandeep_a_k/.nvm/versions/node/v24.15.0/bin:/usr/bin:/bin bash -c '/usr/local/bin/python3 /Users/sandeep_a_k/src/fieldkit/platform/email-agent/scripts/check_email.py --source cron' >> /Users/sandeep_a_k/src/fieldkit/logs/cron.log 2>&1
```

(schedule, `PATH`, and log redirect unchanged — only the bare `python3` became
the explicit `/usr/local/bin/python3` path, matching the other two entries.)

Confirmed via live `tail -f logs/cron.log` across subsequent ticks: no more
`ModuleNotFoundError`, clean runs.

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

**Root-caused as a timing gap, not a code bug.** Live investigation ruled out
the code:

- `check_email.py`'s `_load_env()` parses `.env` generically with no
  key whitelist — nothing about `TELEGRAM_BOT_TOKEN` specifically that could
  be mishandled.
- Call ordering was correct — `_load_env()` runs before any `_telegram()`
  call.
- No cwd sensitivity — reproduced cron's faithful environment with `env -i`
  and confirmed the script resolves `.env` the same way regardless of
  invocation directory.

The actual cause: the `.env` fix landed roughly 12 seconds after a cron tick
had already started and read the old (token-less) file — a single
one-cycle-old snapshot, not a defect. The very next tick, once the edited
file was in place before the tick started, succeeded cleanly.

**Fixed** by adding the missing `TELEGRAM_BOT_TOKEN` value to
`platform/email-agent/.env` — copied from `~/.hermes/.env`, which already
held a working value for the same bot (reused from #6's setup, see
[`02-gateway-setup.md`](02-gateway-setup.md)). Verified clean across live
cron ticks afterward.

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

## 6. Debugging-hygiene note added

Issue #27's remaining acceptance item — a durable note instructing agents
never to raw-dump a file known to hold secrets — is added in this PR to
[`CONTRIBUTING.md`](../../../CONTRIBUTING.md)'s existing "Security & Secrets"
section, the most natural existing home for it in this repo. Summary: use
presence/length-only checks (`grep -c '^KEY='`, `awk -F= '/^KEY=/{print
length($2)}'`) instead of `cat`/`xxd`/`tail -c`/`hexdump` on `.env` files or
credential stores.

## SC-001 — Final Status

[`05-cron-verification.md`](05-cron-verification.md) left SC-001 (cron
scripts run correctly with Hermes installed and running, with no OpenClaw
dependency) only partially met, blocked specifically on the live
`openclaw message send` call sites in `check_approval.py` and
`check_email.py` (its own "FR-003" section) and deferred the
button-callback race to #14. With everything in this doc:

- **OpenClaw dependency**: zero. #24 replaced every live call site with
  direct Telegram Bot API calls before this doc's removal step ran, and §1
  above confirms OpenClaw itself is no longer installed on the machine at
  all — there is no fallback path left to depend on it.
- **Cron scripts run clean with Hermes running**: confirmed by live log
  evidence, not just claims — §3's `cron.log` tail showed
  `check_email.py`'s `ModuleNotFoundError` gone after the interpreter-path
  fix, and §4's cron ticks showed the `TELEGRAM_BOT_TOKEN` warning gone
  after the `.env` fix, both observed while `ai.hermes.gateway` was up and
  polling.
- **Repo-wide grep**: clean per §2, re-run fresh for this doc rather than
  citing an earlier pass.

**SC-001 is genuinely satisfied.** The one item `05-cron-verification.md`
flagged as out of SC-001's scope and explicitly deferred to #14 — the
Hermes/cron-leg `getUpdates` offset race on `check_approval.py`'s
button-callback path — is a separate, already-tracked concern (see that
doc's "Acceptance Criteria — Final Status," item 4) and is unaffected by
anything in this doc; it is not part of what SC-001 measures.
