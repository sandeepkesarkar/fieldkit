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
be filled in — copy from `.env.example` first) and:

1. Writes `CLIENT_NAME=<client-name>` into the repo-root `fieldkit/.env`
   (upsert — replaces any prior value, never duplicates or appends a
   second line).
2. Backs up `~/.hermes/.env`, then writes that client's `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_ALLOWED_USERS`, and provider API key into it.
3. Forces the sticky profile to `default` (`hermes profile use default`,
   guarding against a stray `hermes profile use <other>` left over from
   past experimentation) and runs `hermes config set` for
   `model.provider`, `model.default`, and `skills.external_dirs` — always
   against the default profile, never a named one.
4. Restarts the default gateway (`launchctl kickstart -k
   gui/$(id -u)/ai.hermes.gateway`, falling back to `hermes gateway restart`
   if `launchctl` isn't on `PATH`).
5. If it finds any leftover profile directories under `~/.hermes/profiles/`
   (pre-#61 state — e.g. `mercury`, `venus`), prints exact retirement
   commands for a human to run. **It never touches those directories
   itself** — they're live state from before this architecture decision,
   and mutating already-running, non-default profile state is not this
   script's job (same posture as every other live-infrastructure change in
   this project's history: the automation proposes, a human disposes).

Flags: `--dry-run` (print the plan, touch nothing, run no `hermes`/
`launchctl` commands), `--no-restart` (apply config, skip the gateway
restart).

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
and report them; retiring one is:

```bash
hermes -p <name> gateway stop
hermes -p <name> gateway uninstall
hermes profile delete <name>
```

Run these yourself — `install_client.sh` prints them but never runs them,
since they act on live, already-running state outside this script's scope.

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
