#!/usr/bin/env bash
# install_client.sh — switch which client is the active install (issue #61).
#
# Architecture (permanent decision, superseding the concurrent per-client-
# Hermes-profile model built for #11/#12 and the root cause of #59): this
# fieldkit checkout runs exactly ONE client at a time. The code stays
# multi-client (clients/<name>/ all coexist as source-of-truth config per
# client, forever); what this script does is make ONE of them "active" — on
# this machine, right now — by copying its config into the two places the
# running system actually reads:
#
#   1. The repo-root fieldkit/.env — CLIENT_NAME, FIELDKIT_ROOT.
#   2. Hermes's single DEFAULT profile (~/.hermes/.env, ~/.hermes/config.yaml)
#      — the client's Telegram bot token/allowlist, model provider/key, skill
#      discovery dirs, AND CLIENT_NAME itself (see "Why CLIENT_NAME is also
#      written into Hermes's own .env" below — this is the fix for issue #59
#      cross-vendor review finding Engineering-6, the actual crux of whether
#      #59 is closed).
#
# SECURITY POSTURE (this script switches live production credentials —
# treat every change here as security-critical, not a quick patch):
#   - umask 077 below means every file/dir this script creates defaults to
#     0600/0700; every credential-bearing file gets an explicit chmod besides.
#   - No secret value is ever passed as a command-line argument to another
#     process (ps/process-listing exposure) — file rewriting uses awk with
#     only KEY NAMES (never values) in the awk program text, and `printf`
#     (a shell builtin, not a forked process) to append fresh KEY=value
#     lines to files under redirection, never as another command's argv.
#   - The install is staged in temp files, validated, then atomically
#     rename(2)'d into place — a failure before that point touches no live
#     file at all.
#   - The client name is validated against a strict allowlist pattern before
#     it ever touches a path, and the resolved client directory is checked
#     (post symlink-resolution) to actually live under clients/ — no path
#     traversal, no symlink escape.
#   - The gateway is stopped BEFORE any file is touched and only started
#     again after every write and every `hermes config set` call has
#     succeeded — there is never a window where two gateways (old client,
#     new client) are both live and reachable, and never a window where a
#     running gateway observes a half-written config file.
#   - A single mkdir-based lock file serializes concurrent installer runs —
#     a second invocation refuses immediately rather than racing the first.
#
# Why CLIENT_NAME is also written into Hermes's own .env:
#   process_photos.py (and check_approval.py, upload_facebook.py) resolve
#   CLIENT_NAME with `load_dotenv(root_env, override=False)` — an ALREADY-SET
#   CLIENT_NAME in the process environment wins over whatever the root .env
#   says (issue #45/PR #57's contract, kept deliberately as an ad-hoc
#   single-invocation test override). That means a STALE CLIENT_NAME already
#   present in a process's inherited environment — e.g. carried in a Hermes
#   gateway process's own os.environ before this script ran — would
#   otherwise silently outrank the client this script just installed. Fixed
#   at the source: Hermes's own env_loader.py reloads <HERMES_HOME>/.env
#   into the gateway process's os.environ with override=True on every turn
#   (verified against this machine's installed Hermes; see
#   platform/docs/hermes/09-per-client-model-profiles.md) — so writing
#   CLIENT_NAME into ~/.hermes/.env here means the gateway's own environment
#   is forcibly corrected to the newly-installed client on its very next
#   reload, REGARDLESS of what CLIENT_NAME it inherited before. Every skill-
#   dispatch subprocess Hermes spawns then inherits that corrected value.
#   See test_install_client.py's
#   test_stale_ambient_client_name_does_not_survive_hermes_reload for the
#   proof, modeling this exact reload mechanism.
#
# Usage:
#   install_client.sh <client-name> [--dry-run] [--no-restart]
#
#   <client-name>   Name of a clients/<name>/ directory with a filled-in
#                   src/photo-agent/.env (copy from .env.example first).
#                   Must match ^[A-Za-z0-9_-]+$ — no path separators, no
#                   leading dot, nothing else.
#   --dry-run       Print what would change; touch no files, run no
#                   hermes/launchctl commands, take no lock.
#   --no-restart    Do everything except start the gateway again at the end
#                   (useful when scripting several steps).
#
# Required environment overrides for testing (both default from real paths
# when unset, so a normal human invocation needs neither):
#   FIELDKIT_ROOT   Defaults to this script's own repo checkout.
#   HERMES_HOME     Defaults to ~/.hermes. Point this at a scratch directory
#                    to test against without touching real Hermes state.
#
# Every external command that actually changes live state (`hermes`) is
# invoked via PATH lookup, not a hardcoded path — tests shadow it with a
# stub executable placed earlier on PATH, so the file-write, locking, and
# command-sequencing logic below is exercised for real without ever
# touching this machine's real ~/.hermes or its real launchd services.

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage: install_client.sh <client-name> [--dry-run] [--no-restart]

Switches the single active fieldkit install to <client-name>: stops the
Hermes gateway, atomically rebuilds the repo-root .env (CLIENT_NAME,
FIELDKIT_ROOT) and Hermes's default profile .env (Telegram token/allowlist,
CLIENT_NAME, and only the selected model provider's API key — every other
managed key, including a stale provider key from a prior client, is
removed, not merely left alone) from clients/<client-name>/src/photo-agent/.env,
applies the model/skill config, then starts the gateway again.

  --dry-run      Print the plan; change nothing, take no lock.
  --no-restart   Apply config changes but leave the gateway stopped.
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

# --- Security: strict client-name allowlist, before CLIENT touches any path ---
# No path separators, no "..", no leading dot, no whitespace, no shell
# metacharacters — only what a real `clients/<name>/` directory name is.
if ! [[ "$CLIENT" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "ERROR: invalid client name '$CLIENT' — must match ^[A-Za-z0-9_-]+\$ (letters, digits, underscore, hyphen only; no path separators or '..')" >&2
  exit 1
fi

FIELDKIT_ROOT="${FIELDKIT_ROOT:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

# Resolve FIELDKIT_ROOT itself (symlink-safe) so the "stays under clients/"
# check below compares two canonical paths, not a canonical one against a
# possibly-symlinked one.
FIELDKIT_ROOT="$(cd "$FIELDKIT_ROOT" && pwd -P)"

CLIENTS_ROOT="$FIELDKIT_ROOT/clients"
CLIENT_DIR="$CLIENTS_ROOT/$CLIENT"
CLIENT_ENV="$CLIENT_DIR/src/photo-agent/.env"
ROOT_ENV="$FIELDKIT_ROOT/.env"
HERMES_ENV="$HERMES_HOME/.env"
HERMES_CONFIG="$HERMES_HOME/config.yaml"

if [ ! -d "$CLIENT_DIR" ]; then
  echo "ERROR: no such client: $CLIENT_DIR does not exist" >&2
  exit 1
fi

# --- Security: resolve symlinks and verify the real path still lives under
# clients/ — a malicious or mistaken symlink at clients/<name> pointing
# outside the repo must not be trusted just because the name matched the
# allowlist. -----------------------------------------------------------------
CLIENT_DIR_REAL="$(cd "$CLIENT_DIR" && pwd -P)"
case "$CLIENT_DIR_REAL" in
  "$CLIENTS_ROOT"/*) : ;;
  *)
    echo "ERROR: $CLIENT_DIR resolves (via symlink or otherwise) to $CLIENT_DIR_REAL, which is outside $CLIENTS_ROOT — refusing to trust it" >&2
    exit 1
    ;;
esac
CLIENT_DIR="$CLIENT_DIR_REAL"
CLIENT_ENV="$CLIENT_DIR/src/photo-agent/.env"

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
# Best-effort: the client's own source .env should never be group/world
# readable either — fix it rather than just warn, matching the chmod 600
# convention every .env.example in this repo already documents.
chmod 600 "$CLIENT_ENV" 2>/dev/null || true

# --- Real, minimal, tested dotenv-grammar-aware value extraction -----------
# Handles what naive line-splitting/sed mishandles (issue #62 review
# Engineering-4): an optional leading "export ", surrounding single OR
# double quotes around the value, and a trailing \r (CRLF line endings).
# Does NOT attempt full shell-word-expansion semantics — this repo's own
# .env.example files never need that, and get_client_var below is only ever
# used to read a handful of known, simple values.
#
# get_client_var NAME FILE — returns the LAST matching, uncommented
# "NAME=value" line's unquoted, \r-stripped value. A commented-out line
# (# NAME=...) is deliberately never matched, so a template left as a
# commented example is never read as "set".
get_client_var() {
  local name="$1" file="$2" line value
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${name}=" "$file" 2>/dev/null | tail -n1 || true)"
  [ -n "$line" ] || { printf ''; return 0; }
  # Strip a leading "export " (with its whitespace) and everything up to
  # and including the first '=' -- cut, not a regex substitution, so a
  # '=' inside the value itself is never mistaken for the key/value
  # separator.
  line="${line#"${line%%[![:space:]]*}"}"   # trim leading whitespace
  line="${line#export}"
  line="${line#"${line%%[![:space:]]*}"}"   # trim whitespace after 'export'
  value="${line#*=}"
  value="${value%$'\r'}"                    # strip a trailing CRLF \r
  # Strip one layer of matching surrounding quotes, if present.
  if [[ "$value" == \"*\" && ${#value} -ge 2 ]]; then
    value="${value:1:${#value}-2}"
  elif [[ "$value" == \'*\' && ${#value} -ge 2 ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "$value"
}

TELEGRAM_BOT_TOKEN="$(get_client_var TELEGRAM_BOT_TOKEN "$CLIENT_ENV")"
TELEGRAM_ALLOWED_USERS="$(get_client_var TELEGRAM_ALLOWED_USERS "$CLIENT_ENV")"
HERMES_MODEL_PROVIDER="$(get_client_var HERMES_MODEL_PROVIDER "$CLIENT_ENV")"
HERMES_MODEL_DEFAULT="$(get_client_var HERMES_MODEL_DEFAULT "$CLIENT_ENV")"
HERMES_PROVIDER_API_KEY="$(get_client_var HERMES_PROVIDER_API_KEY "$CLIENT_ENV")"

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

# None of these values may legitimately contain a newline (they're all
# single-line dotenv values) — reject outright rather than let one silently
# corrupt the KEY=value structure of a file we're about to rebuild.
for _val in "$TELEGRAM_BOT_TOKEN" "$TELEGRAM_ALLOWED_USERS" "$HERMES_MODEL_PROVIDER" "$HERMES_MODEL_DEFAULT" "$HERMES_PROVIDER_API_KEY"; do
  case "$_val" in
    *$'\n'*)
      echo "ERROR: a value in $CLIENT_ENV contains an embedded newline — refusing to proceed" >&2
      exit 1
      ;;
  esac
done

# Full, explicit allowlist of every provider-key env var this script knows
# how to manage in Hermes's .env. On every install, ALL of these are
# removed first, then only the one matching the newly-selected provider is
# re-added — this is what actually closes issue #62 review Engineering-2
# ('fully replaces, never merges' was false): a stale OPENAI_API_KEY left
# over from a prior OpenAI-backed client cannot survive a switch to an
# Anthropic-backed one, because it is unconditionally deleted, not merely
# left unupdated.
declare -a ALL_PROVIDER_KEY_VARS=(ANTHROPIC_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY)

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
echo "  repo root:               $FIELDKIT_ROOT"
echo "  root .env:                $ROOT_ENV  (CLIENT_NAME -> $CLIENT)"
echo "  hermes profile:            default  ($HERMES_ENV)"
echo "  hermes model:               $HERMES_MODEL_PROVIDER / $HERMES_MODEL_DEFAULT"
echo "  hermes provider key:        $PROVIDER_KEY_VAR (***, not printed)"
echo "  telegram bot token:         *** (not printed)"
echo "  telegram allowed users:     *** (not printed — access-control metadata, not shown even in --dry-run)"
echo "  skill dirs:                 [\"$FIELDKIT_ROOT/platform/photo-agent/skills\"]"
echo

if [ "$DRY_RUN" -eq 1 ]; then
  echo "--dry-run: no files written, no lock taken, no hermes/gateway commands run."
  exit 0
fi

# --- Preflight: required commands and target-directory writability, before
# generating or touching anything secret. -------------------------------
command -v hermes >/dev/null 2>&1 || {
  echo "ERROR: 'hermes' is not on PATH — cannot configure or restart the gateway" >&2
  exit 1
}
mkdir -p "$(dirname "$ROOT_ENV")" "$HERMES_HOME"
# HERMES_HOME is a credentials-only directory this script owns end to end —
# safe (and required, security-wise) to lock it down to 0700. dirname(ROOT_ENV)
# is the fieldkit repo checkout itself, which this script must NOT chmod —
# doing so would break git and every other tool that expects a normal repo
# directory. Its writability is still checked below, just not its mode.
chmod 700 "$HERMES_HOME" 2>/dev/null || true
for _dir in "$(dirname "$ROOT_ENV")" "$HERMES_HOME"; do
  _probe="$_dir/.install_client_write_test.$$"
  if ! ( : > "$_probe" ) 2>/dev/null; then
    echo "ERROR: $_dir is not writable — aborting before any change" >&2
    exit 1
  fi
  rm -f "$_probe"
done

# --- Locking: serialize concurrent installer runs (issue #62 review
# Engineering-3). mkdir is atomic on every POSIX filesystem this project
# targets and needs no external `flock` binary (not present by default on
# macOS, unlike Linux). A second invocation refuses immediately rather than
# silently interleaving writes with the first. ------------------------------
LOCK_DIR="$HERMES_HOME/.install_client.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "ERROR: another install_client.sh appears to be running (lock held: $LOCK_DIR)." >&2
  echo "If no installer is actually running, remove the stale lock and retry: rmdir '$LOCK_DIR'" >&2
  exit 1
fi
_release_lock() { rmdir "$LOCK_DIR" 2>/dev/null || true; }
trap _release_lock EXIT

# --- Rebuild a dotenv file: strip every line assigning any managed key
# (handles a bare KEY=, an "export KEY=", and a leading '#'-commented one --
# deletion is by KEY NAME MATCH ONLY, via awk with no secret values ever
# embedded in the awk program text, so this never exposes a credential via
# process argv), keeping every other line byte-for-byte, then append fresh
# KEY=value lines (via a shell builtin `printf` redirected to a file
# descriptor already open on the temp file -- never as another process's
# command-line argument) for exactly the keys this install wants set.
#
# rebuild_managed_env SRC_FILE_OR_EMPTY DEST_TMP_FILE MANAGED_KEYS_CSV
#   then the caller appends KEY=value pairs itself via `emit_kv`.
# ---------------------------------------------------------------------------
_rebuild_strip_managed_keys() {
  local src="$1" dest="$2" keys_regex="$3"
  if [ -n "$src" ] && [ -f "$src" ]; then
    # Normalize CRLF -> LF while filtering, so a stray \r on a managed-key
    # line can't dodge the match, and so the output is uniformly LF.
    awk -v pat="^[[:space:]]*(export[[:space:]]+)?(${keys_regex})[[:space:]]*=" \
      '{ sub(/\r$/, ""); if ($0 !~ pat) print }' "$src" > "$dest"
  else
    : > "$dest"
  fi
}

_emit_kv() {
  local dest="$1" key="$2" value="$3"
  printf '%s=%s\n' "$key" "$value" >> "$dest"
}

# Staging temp files MUST live on the same filesystem/device as their final
# target — `mv` (rename(2)) is only atomic within one filesystem; a temp
# file in system /tmp on a different device would silently fall back to a
# non-atomic copy+unlink, defeating the entire point of staging. So each
# temp file is created via mktemp directly in its own target's directory
# (still 0600 immediately, via umask 077 + explicit chmod below), not a
# single shared scratch dir.
ROOT_ENV_TMP="$(mktemp "$(dirname "$ROOT_ENV")/.install_client.root.XXXXXX")"
HERMES_ENV_TMP="$(mktemp "$HERMES_HOME/.install_client.hermes.XXXXXX")"
trap '_release_lock; rm -f "$ROOT_ENV_TMP" "$HERMES_ENV_TMP"' EXIT

_rebuild_strip_managed_keys "$ROOT_ENV" "$ROOT_ENV_TMP" "CLIENT_NAME|FIELDKIT_ROOT"
_emit_kv "$ROOT_ENV_TMP" "CLIENT_NAME" "$CLIENT"
_emit_kv "$ROOT_ENV_TMP" "FIELDKIT_ROOT" "$FIELDKIT_ROOT"

# CLIENT_NAME is deliberately included in the Hermes-.env managed-key set —
# see the module docstring's "Why CLIENT_NAME is also written into Hermes's
# own .env" section above. All three provider keys are always stripped;
# only the newly-selected one is re-added.
_hermes_managed_keys="TELEGRAM_BOT_TOKEN|TELEGRAM_ALLOWED_USERS|CLIENT_NAME|$(IFS='|'; echo "${ALL_PROVIDER_KEY_VARS[*]}")"
_rebuild_strip_managed_keys "$HERMES_ENV" "$HERMES_ENV_TMP" "$_hermes_managed_keys"
_emit_kv "$HERMES_ENV_TMP" "TELEGRAM_BOT_TOKEN" "$TELEGRAM_BOT_TOKEN"
_emit_kv "$HERMES_ENV_TMP" "TELEGRAM_ALLOWED_USERS" "$TELEGRAM_ALLOWED_USERS"
_emit_kv "$HERMES_ENV_TMP" "CLIENT_NAME" "$CLIENT"
_emit_kv "$HERMES_ENV_TMP" "$PROVIDER_KEY_VAR" "$HERMES_PROVIDER_API_KEY"

chmod 600 "$ROOT_ENV_TMP" "$HERMES_ENV_TMP"

# Validate the staged files before anything live is touched: every managed
# key we intended to set must actually be present exactly once.
for _f_k in "$ROOT_ENV_TMP:CLIENT_NAME" "$ROOT_ENV_TMP:FIELDKIT_ROOT" \
            "$HERMES_ENV_TMP:TELEGRAM_BOT_TOKEN" "$HERMES_ENV_TMP:TELEGRAM_ALLOWED_USERS" \
            "$HERMES_ENV_TMP:CLIENT_NAME" "$HERMES_ENV_TMP:$PROVIDER_KEY_VAR"; do
  _f="${_f_k%%:*}"; _k="${_f_k#*:}"
  _count="$(grep -cE "^${_k}=" "$_f")"
  if [ "$_count" -ne 1 ]; then
    echo "ERROR: internal validation failed — staged $_f has $_count line(s) for $_k (expected exactly 1). Aborting before touching any live file." >&2
    exit 1
  fi
done

# --- Safe gateway transition (issue #62 review Engineering-3, Security-5):
# stop the gateway BEFORE any live file is touched, and only start it again
# after every write and every `hermes config set` call below has
# succeeded. This is also what keeps the documented client-switch sequence
# from ever having two gateways (old client, new client) simultaneously
# live and reachable — see platform/docs/hermes/09-per-client-model-profiles.md's
# "What happened to per-client Hermes profiles?" section for the
# equivalent guidance on retiring a leftover non-default profile FIRST. ---
_gateway_running() {
  # Definitive negatives ("not running", "stopped") are checked FIRST and
  # win, because a naive `grep -qi running` would false-positive on the
  # substring "running" inside "not running". Only a standalone "running"
  # with no negating phrase counts as a positive. Unknown/unparseable
  # output defaults to "not running" (the conservative choice here: the
  # atomic-rename + Hermes's own override=True .env reload on its next
  # turn already make a missed stop safe for THIS install's own files, so
  # erring toward not calling `gateway stop` unnecessarily is preferable
  # to erring toward stopping a gateway status detection got wrong).
  local out
  out="$(HERMES_HOME="$HERMES_HOME" hermes gateway status 2>/dev/null || true)"
  if printf '%s' "$out" | grep -qiE 'not running|stopped|not[[:space:]]+installed'; then
    return 1
  fi
  printf '%s' "$out" | grep -qi 'running'
}

GATEWAY_WAS_RUNNING=0
if _gateway_running; then
  GATEWAY_WAS_RUNNING=1
  echo "Stopping the gateway before making any change..."
  HERMES_HOME="$HERMES_HOME" hermes gateway stop
fi

# Back up whatever currently exists, timestamped and 0600, so a failure
# partway through `hermes config set` below can be rolled back rather than
# leaving mixed old/new config live.
_backup_suffix=".bak.$(date +%Y%m%d%H%M%S)"
HERMES_ENV_BACKUP=""
HERMES_CONFIG_BACKUP=""
if [ -f "$HERMES_ENV" ]; then
  HERMES_ENV_BACKUP="$HERMES_ENV$_backup_suffix"
  cp "$HERMES_ENV" "$HERMES_ENV_BACKUP"
  chmod 600 "$HERMES_ENV_BACKUP"
fi
if [ -f "$HERMES_CONFIG" ]; then
  HERMES_CONFIG_BACKUP="$HERMES_CONFIG$_backup_suffix"
  cp "$HERMES_CONFIG" "$HERMES_CONFIG_BACKUP"
  chmod 600 "$HERMES_CONFIG_BACKUP"
fi

# Atomic rename: same-directory temp files rename(2) onto their targets in
# a single filesystem operation — no reader can ever observe a partially
# written file. If either mv fails, nothing has committed for that file.
mv -f "$ROOT_ENV_TMP" "$ROOT_ENV"
chmod 600 "$ROOT_ENV"
mv -f "$HERMES_ENV_TMP" "$HERMES_ENV"
chmod 600 "$HERMES_ENV"

_rollback_hermes_config_and_fail() {
  echo "ERROR: 'hermes config set' failed — rolling back Hermes's config and leaving the gateway STOPPED." >&2
  if [ -n "$HERMES_CONFIG_BACKUP" ]; then
    cp "$HERMES_CONFIG_BACKUP" "$HERMES_CONFIG"
    echo "  Restored $HERMES_CONFIG from $HERMES_CONFIG_BACKUP" >&2
  fi
  echo "  $HERMES_ENV was already switched to '$CLIENT' and was NOT rolled back (its own content is self-consistent — only the hermes CLI config.yaml sequence failed)." >&2
  echo "  Fix whatever 'hermes config set' failed on, then re-run: install_client.sh $CLIENT" >&2
  if [ "$GATEWAY_WAS_RUNNING" -eq 1 ]; then
    echo "  The gateway was running before this install and is now stopped — start it manually once fixed: hermes gateway start" >&2
  fi
  exit 1
}

# hermes config set operates on the currently-active (sticky) profile —
# force it to "default" first so a stray `hermes profile use <other>` left
# over from earlier session experimentation can't silently redirect these
# writes at the wrong profile.
HERMES_HOME="$HERMES_HOME" hermes profile use default || _rollback_hermes_config_and_fail
HERMES_HOME="$HERMES_HOME" hermes config set model.provider "$HERMES_MODEL_PROVIDER" || _rollback_hermes_config_and_fail
HERMES_HOME="$HERMES_HOME" hermes config set model.default "$HERMES_MODEL_DEFAULT" || _rollback_hermes_config_and_fail
HERMES_HOME="$HERMES_HOME" hermes config set skills.external_dirs "[\"$FIELDKIT_ROOT/platform/photo-agent/skills\"]" || _rollback_hermes_config_and_fail

if [ "$NO_RESTART" -eq 0 ]; then
  HERMES_HOME="$HERMES_HOME" hermes gateway start
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
#
# ORDER MATTERS (issue #62 review Security-5): if any of these still exist
# and are running, retire them FIRST — before relying on this install as
# the only live client identity — so there is never a window where the
# just-installed default-profile gateway AND an old per-client-profile
# gateway are both live and reachable with two different clients'
# credentials at once.
if [ -d "$HERMES_HOME/profiles" ]; then
  stale=()
  for d in "$HERMES_HOME"/profiles/*/; do
    [ -d "$d" ] || continue
    stale+=("$(basename "$d")")
  done
  if [ "${#stale[@]}" -gt 0 ]; then
    echo "NOTE: the following old per-client Hermes profiles still exist on this"
    echo "machine and may still be RUNNING right now. They are no longer used —"
    echo "this project runs one client at a time via the default profile only"
    echo "(issue #61). Retire each one yourself, immediately (not run by this"
    echo "script — live state) — until you do, its gateway may still be live"
    echo "and reachable on its own Telegram bot with its own client's"
    echo "credentials, alongside the default-profile gateway this script just"
    echo "(re)started:"
    echo
    for p in "${stale[@]}"; do
      echo "  hermes -p $p gateway stop"
      echo "  hermes -p $p gateway uninstall"
      echo "  hermes profile delete $p"
      echo
    done
  fi
fi
