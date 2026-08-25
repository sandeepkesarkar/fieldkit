# Per-Client Model Provider Routing — Hermes Profiles

**Supersedes:** the "Switching a client to OpenAI" section of
[`02-gateway-setup.md`](02-gateway-setup.md) (issue #6), which documented an
incorrect/incomplete mechanism, and resolves the open question that section
flagged for #11/#12.

**Written for:** issue #12 (venus, OpenAI-backed demo client), applies
identically to issue #11 (mercury, Anthropic-backed demo client).

## What #6's doc got wrong

`02-gateway-setup.md` originally suggested two options for switching a
client to OpenAI:

1. `hermes config set model.provider openai-codex` — **wrong for this use
   case.** `openai-codex` is Hermes's OAuth ChatGPT/Codex-subscription
   provider (`hermes_cli/providers.py`: `auth_type="oauth_external"`,
   `base_url_override="https://chatgpt.com/backend-api/codex"`). It has
   nothing to do with a plain OpenAI API key.
2. `provider: "custom"` with `base_url: "https://api.openai.com/v1"` —
   **works, but isn't the real mechanism.** Hermes ships a first-class
   overlay for exactly this case, so hand-rolling `custom` + `base_url` is
   unnecessary and easy to get subtly wrong (e.g. omitting the right env var
   name for the key).

It also never mentioned the biggest trap: setting `model.provider: openai`
directly in `config.yaml` does **not** call OpenAI's API. `openai` is
registered as an alias that resolves to the OpenRouter aggregator
(`hermes_cli/providers.py`: `ALIASES = {"openai": "openrouter", ...}`) — a
plain-API-key OpenAI setup that used `provider: openai` would silently bill
through OpenRouter instead of OpenAI directly. (This also explains the
`base_url: https://openrouter.ai/api/v1` sitting alongside
`model.provider: anthropic` in this Mac's current `~/.hermes/config.yaml` —
a leftover from an earlier OpenRouter experiment, unrelated to this feature,
left as-is since editing the default profile's config is out of scope here.)

Finally, it left unresolved how two demo customers on different providers
could coexist, since this Hermes install is one gateway process with one
global config.

## The verified mechanism

**Provider identity for a plain OpenAI API key: `openai-api`**, not `openai`,
not `openai-codex`. Confirmed directly from Hermes's own source
(`hermes_cli/providers.py`):

```python
"openai-api": HermesOverlay(
    transport="codex_responses",
    base_url_override="https://api.openai.com/v1",
    base_url_env_var="OPENAI_BASE_URL",
),
```

and from `hermes_cli/models.py`'s provider registry, which labels it
explicitly: `ProviderEntry("openai-api", "OpenAI API", "OpenAI API
(api.openai.com, API key)")`. `hermes auth list` on this machine already
shows a usable credential for it:

```
openai-api (1 credentials):
  #1  OPENAI_API_KEY       api_key env:OPENAI_API_KEY
```

**Per-client isolation: Hermes profiles**, not the global config and not
`platforms.api_server.extra.model_routes` (that block is real, but it's
scoped to the `api_server` platform — an OpenAI-compatible local proxy — not
a general mechanism for routing different Telegram bots to different
providers). A profile is a complete, isolated `~/.hermes/profiles/<name>/`
directory with its own `config.yaml` (`model.provider`, `model.default`),
its own `.env` (API keys), and its own skills — see `hermes profile --help`
and `docs/design/profile-builder.md` in the Hermes source tree. Verified
empirically on this machine (profile created, configured, and deleted purely
for verification — the default profile, bound to `_demo`'s Telegram bot, was
untouched throughout):

```
$ hermes profile create fk-verify-tmp --no-alias --no-skills
Profile 'fk-verify-tmp' created at /Users/sandeep_a_k/.hermes/profiles/fk-verify-tmp

$ hermes -p fk-verify-tmp config set model.provider openai-api
✓ Set model.provider = openai-api in .../profiles/fk-verify-tmp/config.yaml

$ hermes -p fk-verify-tmp config set model.default gpt-5.1
✓ Set model.default = gpt-5.1 in .../profiles/fk-verify-tmp/config.yaml

$ hermes -p fk-verify-tmp doctor
✗ model.provider 'openai-api' is set but no API key is configured (check ~/.hermes/.env or run 'hermes setup')
```

(The `doctor` failure is expected and correct — this test profile was never
given an `OPENAI_API_KEY`. It confirms `openai-api` is recognized as a valid
provider and that the profile's config is genuinely isolated from the
default profile's.)

## The convention this repo uses

**One Hermes profile per client, named after the client directory.** This
resolves #6's open question directly: `_demo` keeps using the default
profile (already bound to its Telegram bot per #6/PR #16); `venus` and
`mercury` each get their own named profile, each with its own `model.provider`
and its own Telegram bot pair, each independently `hermes -p <client>
gateway install`-able as its own supervised process. Nothing about the
photo-agent pipeline changes — `platform/photo-agent/` scripts never call a
model API directly (video generation is deterministic FFmpeg, not
LLM-driven), so the provider only affects which model executes the
`process-photos` / `check-approval` Hermes skills for that client's Telegram
commands. Because those skills are prose instructions with no room for
provider-specific interpretation (see the SKILL.md files' own "relay
verbatim, do not summarise" instructions), Story 2's "identical skill
behavior across providers" acceptance scenario holds by construction.

| Client | Hermes profile | `model.provider` | `model.default` |
|---|---|---|---|
| `_demo` | `default` | `anthropic` | `claude-sonnet-5` |
| `mercury` (#11) | `mercury` | `anthropic` | (mercury's own choice — not this issue) |
| `venus` (#12) | `venus` | `openai-api` | `gpt-5.5` (confirm current availability with `hermes -p venus model`) |

See `clients/venus/README.md`'s Provider Configuration section for the exact
setup commands for venus specifically.

## A fresh profile does not inherit the default profile's setup

Profiles are fully isolated (`docs/design/profile-builder.md`: "a profile is
just a HERMES_HOME directory"), which means a brand-new profile has *none*
of the three things `02-gateway-setup.md` set up for the default profile:

- **Its Telegram bot token lives in the profile's own `.env`, not this
  repo's client `.env`.** The gateway process reads `TELEGRAM_BOT_TOKEN` from
  whichever `HERMES_HOME` it was started under (`agent/secret_scope.py`), so
  `hermes -p venus gateway install` needs `TELEGRAM_BOT_TOKEN` set in
  `~/.hermes/profiles/venus/.env` — the `TELEGRAM_BOT_TOKEN` in
  `clients/venus/src/photo-agent/.env` is a *different* consumer (it's read
  by the plain Python cron scripts, not by Hermes).
- **Its Telegram authorization allowlist is likewise per-profile, and the
  correct way to verify it is direct inspection, not a log line.**
  `02-gateway-setup.md` documents `TELEGRAM_ALLOWED_USERS` alongside
  `TELEGRAM_BOT_TOKEN` for the default profile's `~/.hermes/.env` for
  exactly this reason — the Telegram adapter's authorization gate reads it
  per-profile (`gateway/authz_mixin.py`,
  `plugins/platforms/telegram/adapter.py`:
  `_scoped_gate_env("TELEGRAM_ALLOWED_USERS")`). `hermes doctor` doesn't
  check this at all (verified by grepping `hermes_cli/doctor.py` for
  `ALLOWED_USERS` — no match), so verify by directly reading the value —
  `grep TELEGRAM_ALLOWED_USERS ~/.hermes/profiles/venus/.env` (safe; it's a
  chat ID, not a secret) — or by having the admin's Telegram account send a
  command and confirm it goes through.

  **An earlier draft of this doc suggested checking `gateway/run.py`'s
  startup warning (`No env user allowlists configured`) for absence as
  proof. That's wrong — verified by reading the actual check:** it's
  `any(os.getenv(v) for v in _builtin_allowed_vars + ...)` across roughly
  twenty platform-specific allowlist env vars (Discord, WhatsApp, Slack,
  Signal, email, SMS, ...), so the warning is suppressed by *any one* of
  them being set, and its absence proves nothing about
  `TELEGRAM_ALLOWED_USERS` specifically.

  **Correcting the security framing too:** a missing/wrong
  `TELEGRAM_ALLOWED_USERS` does **not** mean "the bot is now open to
  anyone." Hermes's Telegram adapter is fail-closed by design — its own
  comment says so directly: `"Fail-closed: no allowlist means deny by
  default"`. Actual open access requires a separate, explicit opt-in
  (`GATEWAY_ALLOW_ALL_USERS=true` or a platform's `*_ALLOW_ALL_USERS` /
  open `dm_policy`), which nothing in this setup sets.

  **But "fail-closed" isn't one symptom — it's two different ones,
  depending on which mistake was made**, traced through
  `gateway/authz_mixin.py::_get_unauthorized_dm_behavior`'s resolution
  order (rules 5–6): with no explicit per-platform/global override, it
  defaults to `"ignore"` **when any allowlist is configured** ("the
  allowlist signals that the owner has deliberately restricted access"),
  and to `"pair"` ("open-gateway default") **only when no allowlist is
  configured at all**. Concretely, for `TELEGRAM_ALLOWED_USERS`:
  - **Left unset entirely:** no allowlist exists, so the admin's own DM —
    unrecognized, same as anyone else's — gets routed to Hermes's
    **pairing flow** (a pairing-code prompt), not a normal response.
  - **Set but wrong/mistyped:** an allowlist *does* exist (just not
    matching the admin's actual chat ID), so Hermes treats this as
    deliberate restriction and **silently ignores** unauthorized senders —
    no pairing prompt, no response, no error.

  Either way the admin locking themselves out, not an unauthenticated
  public bot, is the realistic failure mode — but which of the two above
  they hit tells them which mistake they made.
- **`skills.external_dirs` must be set again, per profile.** The default
  profile's `~/.hermes/config.yaml` pointing at
  `platform/photo-agent/skills` (`03-process-photos-skill.md`) has no effect
  on `venus`'s or `mercury`'s config — each profile needs its own
  `hermes -p <client> config set skills.external_dirs '["~/src/fieldkit/platform/photo-agent/skills"]'`,
  verified with `hermes -p <client> skills list --source local`. Verified
  empirically on this machine against a throwaway profile: `skills list`
  showed nothing until `external_dirs` was set on that profile specifically,
  then showed both `process-photos` and `check-approval` as `local` /
  `enabled`.

## Install/config locations touched (per client profile)

| What | Where |
|---|---|
| Model provider config | `~/.hermes/profiles/<client>/config.yaml` (`model.provider`, `model.default`) |
| Skill discovery | `~/.hermes/profiles/<client>/config.yaml` (`skills.external_dirs`) — must be set per profile, see above |
| Secrets | `~/.hermes/profiles/<client>/.env` (provider API key, **and** that profile's own `TELEGRAM_BOT_TOKEN` **and** `TELEGRAM_ALLOWED_USERS`) |
| Gateway supervisor | a separate launchd service per profile — `hermes -p <client> gateway install` |
| Telegram bot | a separate BotFather bot per client (gateway bot), distinct again from that client's approval bot (issue #29) |
