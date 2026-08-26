#!/usr/bin/env bash
# install_client.sh — switch which client is the active install (issue #61).
#
# Architecture (permanent decision, 2026-08-26, superseding the concurrent
# per-client-Hermes-profile model built for #11/#12 and the root cause of
# #59): this fieldkit checkout runs exactly ONE client at a time. The code
# stays multi-client (clients/<name>/ all coexist as source-of-truth config
# per client, forever); what this script does is make ONE of them "active" —
# on this machine, right now — by copying its config into the two places the
# running system actually reads:
#
#   1. The repo-root fieldkit/.env — CLIENT_NAME, read by every
#      platform/photo-agent/ script (process_photos.py, check_approval.py,
#      upload_facebook.py, ...) to pick which clients/<name>/.../.env to
#      load for everything else (Drive folder, Facebook page, Gmail, ...).
#   2. Hermes's single DEFAULT profile (~/.hermes/.env, ~/.hermes/config.yaml)
#      — the client's Telegram bot token/allowlist, model provider/key, and
#      skill discovery dirs. There is no more "which Hermes profile is this
#      running under" ambiguity to resolve (issue #59's actual bug): with
#      only one profile ever active, CLIENT_NAME's fallback-to-root-.env
#      behavior (issue #45/PR #57) is now ALWAYS correct, because there is
#      only ever one client to fall back to.
#
# This script does NOT touch any Hermes profile other than "default" — in
# particular it never touches ~/.hermes/profiles/mercury or
# ~/.hermes/profiles/venus, which may still exist as leftover live state
# from before this architecture decision. Retiring those is a separate,
# human-run step (this script prints the exact commands at the end if it
# finds any) — mirroring how every other live-infrastructure change in this
# project's history has been handled: the script never mutates
# already-running, non-default profile state on its own judgment.
#
# Usage:
#   install_client.sh <client-name> [--dry-run] [--no-restart]
#
#   <client-name>   Name of a clients/<name>/ directory with a filled-in
#                   src/photo-agent/.env (copy from .env.example first).
#   --dry-run       Print what would change; touch no files, run no
#                   hermes/launchctl commands.
#   --no-restart    Do everything except restart the gateway (useful when
#                   scripting several steps and you want to restart once
#                   at the end yourself).
#
# Required environment overrides for testing (both default from real paths
# when unset, so a normal human invocation needs neither):
#   FIELDKIT_ROOT   Defaults to this script's own repo checkout.
#   HERMES_HOME     Defaults to ~/.hermes. Point this at a scratch directory
#                    to test against without touching real Hermes state.
#
# Every external command that actually changes live state (`hermes`,
# `launchctl`) is invoked via PATH lookup, not a hardcoded path — tests
# shadow both with stub executables placed earlier on PATH, so the file-write
# and command-construction logic below is exercised for real without ever
# touching this machine's real ~/.hermes or its real launchd services.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage: install_client.sh <client-name> [--dry-run] [--no-restart]

Switches the single active fieldkit install to <client-name> by copying its
config from clients/<client-name>/src/photo-agent/.env into the repo-root
.env (CLIENT_NAME) and into Hermes's default profile (~/.hermes/.env,
~/.hermes/config.yaml), then restarts the default gateway.

  --dry-run      Print the plan; change nothing.
  --no-restart   Apply config changes but skip the gateway restart.
EOF
}

DRY_RUN=0
NO_RESTART=0
CLIENT=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-restart) NO_RESTART=1 ;;
    -h|--help) usage; exit 0 ;;
    -*)
      echo "ERROR: unknown flag: $arg" >&2
      usage >&2
      exit 1
      ;;
    *)
      if [ -n "$CLIENT" ]; then
        echo "ERROR: unexpected extra argument: $arg" >&2
        usage >&2
        exit 1
      fi
      CLIENT="$arg"
      ;;
  esac
done

if [ -z "$CLIENT" ]; then
  usage >&2
  exit 1
fi

FIELDKIT_ROOT="${FIELDKIT_ROOT:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

CLIENT_DIR="$FIELDKIT_ROOT/clients/$CLIENT"
CLIENT_ENV="$CLIENT_DIR/src/photo-agent/.env"
ROOT_ENV="$FIELDKIT_ROOT/.env"
HERMES_ENV="$HERMES_HOME/.env"

if [ ! -d "$CLIENT_DIR" ]; then
  echo "ERROR: no such client: $CLIENT_DIR does not exist" >&2
  exit 1
fi

if [ ! -f "$CLIENT_ENV" ]; then
  cat >&2 <<EOF
ERROR: $CLIENT_ENV does not exist.

Copy it from platform/photo-agent/.env.example (or
clients/$CLIENT/src/photo-agent/.env.example if one exists) and fill in
every required value before installing $CLIENT as the active client:
  cp $CLIENT_DIR/src/photo-agent/.env.example $CLIENT_ENV
  chmod 600 $CLIENT_ENV
EOF
  exit 1
fi

# get_var NAME FILE — last matching, uncommented "NAME=value" line's value.
# Intentionally does NOT match commented-out lines (# NAME=...): a template
# left as a commented example must not be read as "set".
get_var() {
  local name="$1" file="$2"
  grep -E "^${name}=" "$file" 2>/dev/null | tail -n1 | cut -d= -f2- || true
}

TELEGRAM_BOT_TOKEN="$(get_var TELEGRAM_BOT_TOKEN "$CLIENT_ENV")"
TELEGRAM_ALLOWED_USERS="$(get_var TELEGRAM_ALLOWED_USERS "$CLIENT_ENV")"
HERMES_MODEL_PROVIDER="$(get_var HERMES_MODEL_PROVIDER "$CLIENT_ENV")"
HERMES_MODEL_DEFAULT="$(get_var HERMES_MODEL_DEFAULT "$CLIENT_ENV")"
HERMES_PROVIDER_API_KEY="$(get_var HERMES_PROVIDER_API_KEY "$CLIENT_ENV")"

missing=()
[ -n "$TELEGRAM_BOT_TOKEN" ] || missing+=("TELEGRAM_BOT_TOKEN")
[ -n "$TELEGRAM_ALLOWED_USERS" ] || missing+=("TELEGRAM_ALLOWED_USERS")
[ -n "$HERMES_MODEL_PROVIDER" ] || missing+=("HERMES_MODEL_PROVIDER")
[ -n "$HERMES_MODEL_DEFAULT" ] || missing+=("HERMES_MODEL_DEFAULT")
[ -n "$HERMES_PROVIDER_API_KEY" ] || missing+=("HERMES_PROVIDER_API_KEY")

if [ "${#missing[@]}" -gt 0 ]; then
  echo "ERROR: $CLIENT_ENV is missing required value(s): ${missing[*]}" >&2
  echo "Fill these in (see platform/photo-agent/.env.example) before installing '$CLIENT'." >&2
  exit 1
fi

# Map the client's declared model provider to the env-var name Hermes reads
# for that provider's API key. Deliberately a small, explicit allowlist
# (issue #61 only needs anthropic/openai-api for mercury/venus) rather than
# a guess — an unrecognized provider fails loudly with instructions, never
# silently skips the key or picks a wrong variable name.
case "$HERMES_MODEL_PROVIDER" in
  anthropic) PROVIDER_KEY_VAR="ANTHROPIC_API_KEY" ;;
  openai-api) PROVIDER_KEY_VAR="OPENAI_API_KEY" ;;
  openrouter) PROVIDER_KEY_VAR="OPENROUTER_API_KEY" ;;
  *)
    cat >&2 <<EOF
ERROR: unrecognized HERMES_MODEL_PROVIDER='$HERMES_MODEL_PROVIDER' in $CLIENT_ENV

install_client.sh only knows the API-key variable name for: anthropic,
openai-api, openrouter. Add a case for '$HERMES_MODEL_PROVIDER' to the
case statement in $0 before using this provider — see
platform/docs/hermes/09-per-client-model-profiles.md for provider identity
notes (e.g. why plain "openai" is NOT what you want).
EOF
    exit 1
    ;;
esac

echo "== install_client.sh: switching the active client to '$CLIENT' =="
echo "  repo root:            $FIELDKIT_ROOT"
echo "  root .env:             $ROOT_ENV  (CLIENT_NAME -> $CLIENT)"
echo "  hermes profile:         default  ($HERMES_ENV)"
echo "  hermes model:            $HERMES_MODEL_PROVIDER / $HERMES_MODEL_DEFAULT"
echo "  hermes provider key:     $PROVIDER_KEY_VAR (***, not printed)"
echo "  telegram bot token:      *** (not printed)"
echo "  telegram allowed users:  $TELEGRAM_ALLOWED_USERS"
echo "  skill dirs:              [\"$FIELDKIT_ROOT/platform/photo-agent/skills\"]"
echo

if [ "$DRY_RUN" -eq 1 ]; then
  echo "--dry-run: no files written, no hermes/launchctl commands run."
  exit 0
fi

# upsert_env_var FILE KEY VALUE — replace an existing "KEY=..." line
# (commented or not) in place, or append a new "KEY=VALUE" line if none
# exists. Portable across BSD sed (macOS) and GNU sed (Linux) via the
# -i.bak-suffix-then-remove form, which both accept identically.
upsert_env_var() {
  local file="$1" key="$2" value="$3"
  local escaped
  escaped=$(printf '%s' "$value" | sed -e 's/[&/\]/\\&/g')
  if [ -f "$file" ] && grep -qE "^#?[[:space:]]*${key}=" "$file"; then
    sed -i.install_client_tmp -E "s|^#?[[:space:]]*${key}=.*|${key}=${escaped}|" "$file"
    rm -f "$file.install_client_tmp"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

mkdir -p "$(dirname "$ROOT_ENV")"
touch "$ROOT_ENV"
upsert_env_var "$ROOT_ENV" "CLIENT_NAME" "$CLIENT"
upsert_env_var "$ROOT_ENV" "FIELDKIT_ROOT" "$FIELDKIT_ROOT"

mkdir -p "$HERMES_HOME"
if [ -f "$HERMES_ENV" ]; then
  cp "$HERMES_ENV" "$HERMES_ENV.bak.$(date +%Y%m%d%H%M%S)"
fi
touch "$HERMES_ENV"
upsert_env_var "$HERMES_ENV" "TELEGRAM_BOT_TOKEN" "$TELEGRAM_BOT_TOKEN"
upsert_env_var "$HERMES_ENV" "TELEGRAM_ALLOWED_USERS" "$TELEGRAM_ALLOWED_USERS"
upsert_env_var "$HERMES_ENV" "$PROVIDER_KEY_VAR" "$HERMES_PROVIDER_API_KEY"

# hermes config set operates on the currently-active (sticky) profile —
# force it to "default" first so a stray `hermes profile use <other>` left
# over from earlier session experimentation can't silently redirect these
# writes at the wrong profile.
HERMES_HOME="$HERMES_HOME" hermes profile use default
HERMES_HOME="$HERMES_HOME" hermes config set model.provider "$HERMES_MODEL_PROVIDER"
HERMES_HOME="$HERMES_HOME" hermes config set model.default "$HERMES_MODEL_DEFAULT"
HERMES_HOME="$HERMES_HOME" hermes config set skills.external_dirs "[\"$FIELDKIT_ROOT/platform/photo-agent/skills\"]"

if [ "$NO_RESTART" -eq 0 ]; then
  if command -v launchctl >/dev/null 2>&1; then
    launchctl kickstart -k "gui/$(id -u)/ai.hermes.gateway" 2>/dev/null \
      || HERMES_HOME="$HERMES_HOME" hermes gateway restart
  else
    HERMES_HOME="$HERMES_HOME" hermes gateway restart
  fi
fi

echo "Installed '$CLIENT' as the active client."
echo

# Leftover non-default profiles (e.g. ~/.hermes/profiles/mercury from
# before #61) are live state this script never touches on its own —
# surface exact retirement commands for a human to run instead. A profile
# is literally just a HERMES_HOME directory (see
# platform/docs/hermes/09-per-client-model-profiles.md), so this is a
# filesystem check, not a fragile parse of `hermes profile list`'s
# human-formatted table.
if [ -d "$HERMES_HOME/profiles" ]; then
  stale=()
  for d in "$HERMES_HOME"/profiles/*/; do
    [ -d "$d" ] || continue
    stale+=("$(basename "$d")")
  done
  if [ "${#stale[@]}" -gt 0 ]; then
    echo "NOTE: the following old per-client Hermes profiles still exist on this"
    echo "machine. They are no longer used — this project runs one client at a"
    echo "time via the default profile only (issue #61). Retire each one"
    echo "yourself (not run by this script — live state):"
    echo
    for p in "${stale[@]}"; do
      echo "  hermes -p $p gateway stop"
      echo "  hermes -p $p gateway uninstall"
      echo "  hermes profile delete $p"
      echo
    done
  fi
fi
