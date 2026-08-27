# Per-Client Model Provider Routing — Single-Install Model (issue #61)

**Supersedes:** the original version of this document (per-client Hermes
profiles, `hermes profile create <name>`, one running gateway per client),
which itself superseded the "Switching a client to OpenAI" section of
[`02-gateway-setup.md`](02-gateway-setup.md) (issue #6). That per-profile
design was scope creep beyond this project's real deployment model and was
the direct root cause of [issue #59](https://github.com/sandeepkesarkar/fieldkit/issues/59):
a live `/process_photos` invocation in mercury's Telegram bot silently
resolved `CLIENT_NAME` against `_demo`'s credentials, because the concurrent
per-client-profile setup gave the skill-dispatch subprocess no reliable way
to know which profile it was running under. See
[issue #61](https://github.com/sandeepkesarkar/fieldkit/issues/61) for the
full architecture decision this document now reflects.

**Written for:** issue #11/#12 (mercury, venus) originally; this revision
generalizes it to every client this repo scaffolds.

## The architecture decision (permanent)

**This fieldkit install runs exactly ONE client at a time.** The code stays
multi-client forever — `clients/_demo/`, `clients/mercury/`, `clients/venus/`,
`clients/_construction_co/`, `clients/_template/` all continue to coexist in
the repo as source-of-truth config per client. What's single is the
*deployment*: on any one machine, at any one time, only one client's config
is "installed" — copied into the two places the running system actually
reads:

1. **The repo-root `fieldkit/.env`** — `CLIENT_NAME`, read by every
   `platform/photo-agent/` script to pick which `clients/<name>/.../.env`
   to load for everything else (Drive folder, Facebook page, Gmail, ...).
2. **Hermes's single DEFAULT profile** (`~/.hermes/.env`,
   `~/.hermes/config.yaml`) — that client's Telegram bot token/allowlist,
   model provider/key, and skill discovery dirs.

There is no more "which Hermes profile is a live skill invocation running
under" question to answer, because there is only ever one profile — the
default — and only ever one client's config installed anywhere on the
machine. This is what makes `CLIENT_NAME`'s fallback-to-root-`.env` behavior
(issue #45/PR #57) **always correct**: the root `.env` can never disagree
with "the currently active client," because there is only one active client,
full stop. Issue #59's entire failure precondition — two clients' config
being simultaneously live and a subprocess guessing wrong between them —
cannot occur under this model.

## Switching the active client: `install_client.sh`

```bash
platform/photo-agent/scripts/install_client.sh <client-name>
```

Reads `clients/<client-name>/src/photo-agent/.env` (must already exist and
be filled in — copy from `.env.example` first) and, in this exact order
(kept in sync with the script itself — if this list and the code ever
disagree, the code is authoritative and this doc has drifted):

1. **Validates and canonicalizes paths first, with zero side effects.**
   The client name is checked against `^[A-Za-z0-9_-]+$` before it ever
   touches a path; both the resolved client directory AND the resolved
   `.env` file itself (not just its parent directory) are symlink-resolved
   and verified to still live under `clients/` — a symlink at either level
   pointing outside the repo is rejected outright, never followed. Prints
   the plan and, if `--dry-run` was passed, exits here — before the first
   side-effecting line of any kind (no `mkdir`, no `chmod`, no lock, no
   temp file).
2. **Checks whether any leftover, non-default Hermes profile
   (`~/.hermes/profiles/<name>/` — pre-#61 state, e.g. `mercury`, `venus`)
   has a gateway that's currently running — before ANY mutation of any
   kind, not merely before the live `.env` files.** If any is confirmed
   running, or its status can't be confirmed at all, the install **aborts
   here**, before even the preflight command/writability checks or the
   lock, with the exact retirement commands to run first — never merely
   warns about this after the fact once a new gateway is already up (that
   would recreate the exact two-gateways-live exposure issue #59 was
   about). **It never touches those profiles itself** either way — they're
   live state from before this architecture decision, and mutating
   already-running, non-default profile state is not this script's job
   (same posture as every other live-infrastructure change in this
   project's history: the automation proposes, a human disposes).
3. **Preflight, lock, and staging.** Confirms `hermes` is on `PATH`;
   creates and locks down `HERMES_HOME` (`chmod 700`); confirms both target
   directories are writable; takes a `mkdir`-based lock so a concurrent
   invocation refuses immediately rather than racing; stages a full
   rebuild of both the repo-root `fieldkit/.env` (`CLIENT_NAME`,
   `FIELDKIT_ROOT`) and Hermes's default profile `.env`
   (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`, `CLIENT_NAME`, and only
   the selected provider's API key) as temp files — **not written to their
   live locations yet.** Every managed key is stripped from whatever
   existed before and re-added fresh: a stale `OPENAI_API_KEY` left over
   from a prior OpenAI-backed client cannot survive a switch to an
   Anthropic-backed one, and no key can end up duplicated.
4. **Checks the DEFAULT profile's own gateway status** and stops it first
   if running — an unrecognized/ambiguous status also aborts here rather
   than guessing "not running."
5. **Runs `hermes config set`** (`model.provider`, `model.default`,
   `skills.external_dirs`, always against the forced-sticky `default`
   profile) against a backed-up `config.yaml` (content AND original file
   mode both captured, so a restore can't leave the file more permissive
   than it started — `hermes config set` itself can rewrite `config.yaml`
   via its own atomic write partway through this sequence, which changes
   its mode as a side effect). **Only if every one of these calls
   succeeds** does the script proceed to the next step — a failure here
   rolls `config.yaml` back to exactly what it was (content and mode)
   before this attempt (deleted outright if this run would have created it
   fresh) and leaves **both staged `.env` files completely uncommitted** —
   the live files are untouched, not merely self-consistent with each
   other.
6. **Commits.** Only now, after every fallible step above has succeeded:
   fixes the client's own source `.env` permissions (deferred to here, not
   done at validation time, so a failure before this point never mutates
   it); commits Hermes's `.env` first (it's the file that actually governs
   live skill dispatch — the #59 exposure this script exists to close — so
   if the second commit below ever fails, the file that matters most is
   already correct); then commits the root `.env`. POSIX offers no true
   multi-file transaction, so if the second commit fails after the first
   succeeds, the script does not pretend that can't happen — it prints
   exactly which file is already correct and how to finish the fix by
   re-running.
7. **Re-checks every stale profile's status one more time, immediately
   before actually starting the new default-profile gateway** — closing
   the gap where a stale profile's gateway could start in the window
   between step 2's check and this final moment. Both `.env` files are
   already committed by this point regardless of this recheck's outcome
   (they're correct for the client being installed); what's refused, if a
   stale profile is now found running, is only the new gateway's start —
   this script never itself creates a moment where two gateways with two
   different clients' credentials are both live.

There is no window where one live file reflects the new client's config and
another still reflects the old one, and no window where the gateway
observes a half-written file.

Flags: `--dry-run` (print the plan; makes **zero** filesystem changes of
any kind — no `mkdir`, no `chmod`, no lock, no temp file — and runs no
`hermes` commands), `--no-restart` (apply config, leave the gateway
stopped).

**Required fields in `clients/<name>/src/photo-agent/.env`** (added to
`platform/photo-agent/.env.example` and every client's own `.env.example`):

| Variable | Used for |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Already required for the pipeline scripts themselves — reused as-is for Hermes's gateway bot (one bot serves both, issue #49) |
| `TELEGRAM_ALLOWED_USERS` | Comma-separated Telegram user IDs allowed to command the Hermes gateway bot |
| `HERMES_MODEL_PROVIDER` | e.g. `anthropic`, `openai-api` — see provider identity notes below |
| `HERMES_MODEL_DEFAULT` | Model id for that provider (`hermes model` shows the current picker list) |
| `HERMES_PROVIDER_API_KEY` | That client's own API key for `HERMES_MODEL_PROVIDER` — `install_client.sh` maps it to the correct real env var name (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, ...) before writing it into `~/.hermes/.env` |

Missing any required field, or an unrecognized `HERMES_MODEL_PROVIDER`
(the script only knows the API-key variable name for `anthropic`,
`openai-api`, and `openrouter` — add a case to the script before using
another provider), fails the whole install loudly before touching any
file — never a partial install with some fields switched and others stale.

## Diagnosing a Telegram allowlist problem after installing a client

`hermes doctor` does **not** check the Telegram allowlist at all — verify
it landed with `grep '^TELEGRAM_ALLOWED_USERS=' ~/.hermes/.env` (safe to
inspect directly, it's a chat ID, not a secret). **Do not** rely on the
absence of Hermes's "No env user allowlists configured" startup warning as
proof this specific variable is set: that check is
`any(os.getenv(v) for v in <~20 platform allowlist vars>)` across every
supported platform (`gateway/run.py`) — it's suppressed by *any one* of
them being set, so its absence tells you nothing about
`TELEGRAM_ALLOWED_USERS` specifically.

If the installed client's bot doesn't respond correctly, what you see
depends on *which* mistake was made — these are two different
troubleshooting experiences, not one generic "misconfigured" outcome
(traced in `gateway/authz_mixin.py::_get_unauthorized_dm_behavior`, rules
5–6):

- **`TELEGRAM_ALLOWED_USERS` left unset entirely:** no allowlist exists at
  all, so Hermes falls back to its open-gateway default — the admin's DM
  gets a **pairing-code prompt** instead of a normal response. Expect a
  pairing flow you didn't ask for, not silence.
- **`TELEGRAM_ALLOWED_USERS` set but wrong/mistyped** (e.g. the wrong chat
  ID): an allowlist *does* exist, which Hermes takes as a deliberate access
  restriction — unauthorized senders are **silently ignored**, no pairing
  prompt, no response, nothing. Expect total silence, not an error.

Either way, no unauthenticated third party ever gains access — Hermes's
Telegram adapter is fail-closed by design (`plugins/platforms/telegram/adapter.py`:
"Fail-closed: no allowlist means deny by default"). The one way this
becomes an actual open-access exposure is `GATEWAY_ALLOW_ALL_USERS=true` or
a platform-specific `*_ALLOW_ALL_USERS`/open `dm_policy`, neither of which
`install_client.sh` ever sets.

## Provider identity — still correct, still worth reading

This part of the original investigation is unaffected by the architecture
change and remains the reference for what a given `HERMES_MODEL_PROVIDER`
value actually does. Confirmed directly from Hermes's own source
(`hermes_cli/providers.py`):

- **`openai-api`** — a plain OpenAI API key:
  ```python
  "openai-api": HermesOverlay(
      transport="codex_responses",
      base_url_override="https://api.openai.com/v1",
      base_url_env_var="OPENAI_BASE_URL",
  ),
  ```
  and `hermes_cli/models.py`'s registry labels it explicitly:
  `ProviderEntry("openai-api", "OpenAI API", "OpenAI API (api.openai.com, API key)")`.
- **Bare `openai`** is registered as an alias that resolves to the
  OpenRouter aggregator (`ALIASES = {"openai": "openrouter", ...}`) — a
  plain-API-key OpenAI setup that used `provider: openai` would silently
  bill through OpenRouter instead of OpenAI directly.
- **`openai-codex`** is Hermes's OAuth ChatGPT/Codex-subscription provider
  (`auth_type="oauth_external"`) — unrelated to a plain API key, and not
  something `install_client.sh` supports (it only writes API-key-shaped
  credentials).

## What happened to per-client Hermes profiles?

**Retired for this project's actual usage**, though Hermes itself still
supports `hermes profile create <name>` generally — nothing about this
decision changes Hermes's own capabilities, only how this repo uses them.
The original empirical verification that profiles are real, isolated,
independently-configurable `HERMES_HOME` directories (a throwaway
`fk-verify-tmp` profile created, configured, and deleted on this machine)
is still accurate and was genuinely useful for understanding Hermes's
mechanics — it's just not the mechanism this project uses in production
anymore. If `~/.hermes/profiles/<name>/` directories exist on a machine
from before this decision (mercury, venus), `install_client.sh` will detect
and report them.

**Retirement order matters, and `install_client.sh` now enforces it rather
than merely documenting it** — an earlier version let the install proceed
regardless and only warned about a leftover profile after the fact, which
review correctly identified as recreating exactly the kind of exposure
issue #59 was about, and which was confirmed live on this machine: the
default gateway and `ai.hermes.gateway-mercury` were both found running
simultaneously, well after this architecture had already shifted away from
the per-profile model — a leftover profile doesn't retire itself just
because it's no longer the intended design. A leftover per-client-profile
gateway is a **separate, independent launchd service with its own Telegram
bot** — `install_client.sh` never touches it, in either direction, at any
point in its run. That means if it's still running, it stays fully live and
reachable on its own bot regardless of what `install_client.sh` does to the
default profile — installing a new client does not "take over" from it or
shut it down.

**So: `install_client.sh` checks every leftover per-client profile's
gateway status BEFORE touching anything live, and refuses to proceed at
all if any is confirmed running or its status can't be confirmed** — it
prints the exact retirement commands and exits non-zero rather than
installing anyway. Retire it, then re-run the install:

```bash
hermes -p <name> gateway stop
hermes -p <name> gateway uninstall
hermes profile delete <name>
```

Run these yourself, for every stale profile `install_client.sh` reports,
**before** relying on the newly-installed client's identity as the only
live one — `install_client.sh` prints these commands but never runs them
itself, since they act on live, already-running state outside this
script's scope (see its own module docstring for the full reasoning).

## What if I need to test a non-active client without switching?

`install_client.sh` changes what's *installed* — the live Telegram bot and
gateway a real admin talks to. For a one-off manual test or e2e run against
a client other than the currently-installed one, without disturbing it, use
the inline `CLIENT_NAME=` override (issue #45/PR #57 — still supported,
still tested, see
[`platform/photo-agent/tests/test_client_name_override.py`](../../photo-agent/tests/test_client_name_override.py)):

```bash
CLIENT_NAME=venus FIELDKIT_ROOT=/absolute/path/to/fieldkit \
    python3 platform/photo-agent/scripts/run_e2e_test.py --duration 20
```

This never touches the repo-root `.env` or Hermes's default profile — it's
scoped to that one process only. It does **not** reach live Hermes skill
dispatch (`/process_photos` typed in Telegram) at all, because Hermes's
skill-invoked subprocess never carries an inline override — only
`install_client.sh`'s changes to the root `.env` and Hermes's default
profile affect what a live Telegram command actually does. This is exactly
the distinction issue #59 exposed: the inline-override escape hatch was
never the live skill-dispatch path to begin with, and is not a substitute
for actually installing a client.
