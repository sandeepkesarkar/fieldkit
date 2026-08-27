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
#   - --dry-run makes ZERO filesystem changes of any kind (no mkdir, no
#     chmod, no lock, no temp file) — it exits immediately after printing
#     the plan, before the first side-effecting line runs.
#   - Both live .env files (root and Hermes's) are staged in temp files and
#     are NOT committed (atomically renamed into place) until AFTER every
#     fallible `hermes config set` call has already succeeded — a failure
#     anywhere in that sequence leaves BOTH .env files completely untouched
#     (the temp files are discarded), and rolls config.yaml back to exactly
#     what it was before this run (deleted outright if this run created it
#     fresh, restored from a snapshot otherwise). There is no window where
#     one live file reflects the new client and another still reflects the
#     old one.
#   - The client name is validated against a strict allowlist pattern before
#     it ever touches a path, and BOTH the resolved client directory and the
#     resolved client .env FILE (a symlink at either level, not just the
#     directory) are canonicalized and verified to still live under
#     clients/ before anything reads or touches them — no path traversal,
#     no symlink escape at either level.
#   - Gateway status (this profile's, and every stale non-default profile's)
#     is checked BEFORE any live file is touched. An AMBIGUOUS status
#     (command failure, unrecognized output) is treated as "assume running"
#     and aborts the whole install — never as "assume stopped" — because
#     proceeding on a wrong guess is exactly the two-gateways-live exposure
#     this script exists to prevent. Any stale, non-default profile
#     confirmed running aborts the install outright, with the exact
#     retirement commands, rather than merely warning about it after the
#     fact once the new gateway is already up.
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
#   into the gateway process's os.environ with override=True on every turn,
#   for a NON-MULTIPLEXED gateway (a separate launchd service/process per
#   profile, no shared multiplexing) — which is how this Mac Mini runs the
#   default profile, and is this project's whole deployment model under
#   issue #61's single-install architecture. (Hermes's own
#   `agent.secret_scope.is_multiplex_active()` check means a MULTIPLEXED
#   gateway skips that global os.environ reload entirely and resolves
#   credentials from a per-turn routed secret scope instead — verified by
#   reading gateway/run.py's `_reload_runtime_env_preserving_config_authority()`
#   directly; irrelevant to this project's actual deployment, called out
#   here only so this claim doesn't get cited outside that scope.) So
#   writing CLIENT_NAME into ~/.hermes/.env here means the gateway process's
#   own environment is forcibly corrected to the newly-installed client on
#   its very next reload, REGARDLESS of what CLIENT_NAME it inherited
#   before. Every skill-dispatch subprocess Hermes spawns then inherits
#   that corrected value. See test_install_client.py's
#   test_stale_ambient_client_name_does_not_survive_real_hermes_reload,
#   which invokes Hermes's actual installed `load_hermes_dotenv()` (not a
#   reimplementation) against an isolated scratch HERMES_HOME to prove this.
#
# Usage:
#   install_client.sh <client-name> [--dry-run] [--no-restart]
#
#   <client-name>   Name of a clients/<name>/ directory with a filled-in
#                   src/photo-agent/.env (copy from .env.example first).
#                   Must match ^[A-Za-z0-9_-]+$ — no path separators, no
#                   leading dot, nothing else.
#   --dry-run       Print what would change. Makes ZERO filesystem changes
#                   (see SECURITY POSTURE above) and runs no hermes/gateway
#                   commands.
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

Switches the single active fieldkit install to <client-name>: verifies no
stale non-default Hermes profile gateway is currently running, stops the
default gateway, stages a full rebuild of the repo-root .env (CLIENT_NAME,
FIELDKIT_ROOT) and Hermes's default profile .env (Telegram token/allowlist,
CLIENT_NAME, and only the selected model provider's API key — every other
managed key, including a stale provider key from a prior client, is
removed, not merely left alone) from clients/<client-name>/src/photo-agent/.env,
applies the model/skill config, commits both staged files atomically only
after that config apply fully succeeds, then starts the gateway again.

  --dry-run      Print the plan. Makes ZERO filesystem changes of any kind
                 (no mkdir, no chmod, no lock, no temp file) and runs no
                 hermes/gateway commands.
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
# checks below compare canonical paths on both sides, not a canonical one
# against a possibly-symlinked one.
FIELDKIT_ROOT="$(cd "$FIELDKIT_ROOT" && pwd -P)"

# Portable full symlink resolution for a FILE (not just a directory —
# `cd dir && pwd -P` only resolves symlinks in the directory chain, not a
# symlink at the final path component itself, which is exactly the gap a
# prior version of this script had: `clients/<name>/.../.env` being a
# symlink to an arbitrary outside file was followed by `[ -f ]`, the value
# parser, and `chmod 600` without ever being rejected). python3 is already
# a hard dependency of every sibling script in this repo, so this is a
# reasonable, portable choice on both macOS (whose `readlink` lacks GNU's
# `-f`) and Linux.
_realpath() {
  python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

CLIENTS_ROOT="$FIELDKIT_ROOT/clients"
CLIENT_DIR="$CLIENTS_ROOT/$CLIENT"
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

if [ ! -e "$CLIENT_ENV" ]; then
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

# --- Security: resolve the .env FILE's own real path (not just its parent
# directory) and verify it too stays under the already-verified client
# directory — closes the exact gap a prior version left open: an in-tree
# symlink AT the .env path itself (clients/<name>/src/photo-agent/.env ->
# /somewhere/else) was previously followed transparently by every
# downstream read/chmod. All reads below use CLIENT_ENV_REAL, the verified
# resolved path, never the possibly-symlinked original. ---------------------
CLIENT_ENV_REAL="$(_realpath "$CLIENT_ENV")"
case "$CLIENT_ENV_REAL" in
  "$CLIENT_DIR"/*) : ;;
  *)
    echo "ERROR: $CLIENT_ENV resolves (via symlink or otherwise) to $CLIENT_ENV_REAL, which is outside $CLIENT_DIR — refusing to trust it" >&2
    exit 1
    ;;
esac
if [ ! -f "$CLIENT_ENV_REAL" ]; then
  echo "ERROR: $CLIENT_ENV resolves to $CLIENT_ENV_REAL, which is not a regular file — refusing to trust it" >&2
  exit 1
fi
CLIENT_ENV="$CLIENT_ENV_REAL"

# --- Real, minimal, tested dotenv-grammar-aware value extraction -----------
# Handles what naive line-splitting/sed mishandles: an optional leading
# "export ", surrounding single OR double quotes around the value, and a
# trailing \r (CRLF line endings). Does NOT attempt full shell-word-
# expansion semantics — this repo's own .env.example files never need
# that, and get_client_var below is only ever used to read a handful of
# known, simple values.
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
# re-added — a stale OPENAI_API_KEY left over from a prior OpenAI-backed
# client cannot survive a switch to an Anthropic-backed one, because it is
# unconditionally deleted, not merely left unupdated.
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

# --- SECURITY: --dry-run exits HERE, before the first side-effecting line
# of this script runs (no chmod, no mkdir, no lock, no temp file, no
# hermes/gateway command). Every remaining line below this point performs a
# real, live-state-affecting action. ----------------------------------------
if [ "$DRY_RUN" -eq 1 ]; then
  echo "--dry-run: no files written, no permissions changed, no lock taken, no hermes/gateway commands run."
  exit 0
fi

# The client's own source .env should never be group/world readable either
# — fix it rather than just warn, matching the chmod 600 convention every
# .env.example in this repo already documents. Only reached for a real
# (non-dry-run) install, and only ever applied to CLIENT_ENV_REAL (the
# already-verified, in-bounds resolved path) — never to whatever a symlink
# might have pointed at, since that case was already rejected above.
chmod 600 "$CLIENT_ENV" 2>/dev/null || true

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

# --- Locking: serialize concurrent installer runs. mkdir is atomic on every
# POSIX filesystem this project targets and needs no external `flock`
# binary (not present by default on macOS, unlike Linux). A second
# invocation refuses immediately rather than silently interleaving writes
# with the first. -------------------------------------------------------
LOCK_DIR="$HERMES_HOME/.install_client.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "ERROR: another install_client.sh appears to be running (lock held: $LOCK_DIR)." >&2
  echo "If no installer is actually running, remove the stale lock and retry: rmdir '$LOCK_DIR'" >&2
  exit 1
fi
_release_lock() { rmdir "$LOCK_DIR" 2>/dev/null || true; }
trap _release_lock EXIT

# --- Rebuild a dotenv file: strip every line assigning any managed key
# (handles a bare KEY=, an "export KEY=", and normalizes CRLF -> LF so a
# stray \r can't dodge the match — deletion is by KEY NAME MATCH ONLY, via
# awk with no secret values ever embedded in the awk program text, so this
# never exposes a credential via process argv), keeping every other line
# byte-for-byte, then append fresh KEY=value lines (via a shell builtin
# `printf` redirected to a file already open on the temp file -- never as
# another process's command-line argument) for exactly the keys this
# install wants set. --------------------------------------------------------
_rebuild_strip_managed_keys() {
  local src="$1" dest="$2" keys_regex="$3"
  if [ -n "$src" ] && [ -f "$src" ]; then
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
# temp file is created via mktemp directly in its own target's directory.
#
# The trap is (re-)installed immediately after EACH mktemp call, not once
# at the end after both — a failure on the SECOND mktemp under `set -e`
# would otherwise exit before any trap covering the first temp file existed,
# leaking it.
ROOT_ENV_TMP="$(mktemp "$(dirname "$ROOT_ENV")/.install_client.root.XXXXXX")"
trap '_release_lock; rm -f "$ROOT_ENV_TMP"' EXIT
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
# key we intended to set must actually be present exactly once. Neither
# ROOT_ENV_TMP nor HERMES_ENV_TMP is committed yet — they stay as
# uncommitted temp files (cleaned up by the trap on any exit) until the
# fallible `hermes config set` sequence below has fully succeeded. This is
# the actual fix for "fully transactional install": a failure ANYWHERE
# before the two `mv -f` lines near the bottom of this script means BOTH
# live .env files remain 100% untouched, not just individually atomic.
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

# --- Gateway status: FAIL CLOSED on anything ambiguous. A command failure
# or unrecognized output is treated as "assume running" for a stale
# non-default profile (the more dangerous unknown to get wrong is "assumed
# retired when it's actually still live") and as "abort, don't guess" for
# the default profile this script is about to reconfigure — proceeding on
# a wrong guess in either direction is exactly the kind of exposure this
# script exists to prevent, so an ambiguous read is never treated as
# license to proceed. ---------------------------------------------------
# _gateway_status [extra hermes args...] -- echoes one of:
#   running | not-running | ambiguous
# Never mutates anything -- pure query.
_gateway_status() {
  local out rc
  out="$(HERMES_HOME="$HERMES_HOME" hermes "$@" gateway status 2>&1)"; rc=$?
  if [ $rc -ne 0 ]; then
    echo "ambiguous"; return
  fi
  if printf '%s' "$out" | grep -qiE 'not running|stopped|not[[:space:]]+installed'; then
    echo "not-running"; return
  fi
  if printf '%s' "$out" | grep -qi 'running'; then
    echo "running"; return
  fi
  echo "ambiguous"
}

# --- Stale non-default Hermes profiles (e.g. ~/.hermes/profiles/mercury from
# before issue #61): checked and, if any is confirmed running, this install
# is ABORTED here — before touching anything live — rather than merely
# warned about after the fact once the new default-profile gateway is
# already up. A leftover per-client-profile gateway is a fully separate,
# independently-running launchd service this script never starts or stops
# in any other code path; if it's still live, it stays reachable on its
# own Telegram bot with its own client's credentials regardless of what
# this script does to the default profile, so the two-gateways-live
# exposure this script exists to prevent can only actually be prevented by
# refusing to proceed until it's retired. See
# platform/docs/hermes/09-per-client-model-profiles.md for the full
# writeup and the exact retirement commands (printed below too). ------------
# Declared unconditionally (not just inside the `if -d profiles` block
# below) so referencing their length later under `set -u` never hits an
# unbound-variable error when no profiles/ directory exists at all.
stale_running=()
stale_not_running=()
stale_ambiguous=()
if [ -d "$HERMES_HOME/profiles" ]; then
  for d in "$HERMES_HOME"/profiles/*/; do
    [ -d "$d" ] || continue
    p="$(basename "$d")"
    case "$(_gateway_status -p "$p")" in
      running) stale_running+=("$p") ;;
      not-running) stale_not_running+=("$p") ;;
      *) stale_ambiguous+=("$p") ;;
    esac
  done
  if [ "${#stale_running[@]}" -gt 0 ] || [ "${#stale_ambiguous[@]}" -gt 0 ]; then
    echo "ERROR: refusing to install '$CLIENT' — at least one stale, non-default" >&2
    echo "Hermes profile still has a gateway that is running (or whose status" >&2
    echo "could not be confirmed, which this script treats the same way):" >&2
    for p in "${stale_running[@]:-}"; do [ -n "$p" ] && echo "  - $p (confirmed RUNNING)" >&2; done
    for p in "${stale_ambiguous[@]:-}"; do [ -n "$p" ] && echo "  - $p (status could not be confirmed)" >&2; done
    echo >&2
    echo "This is exactly the two-gateways-live exposure issue #59 was about —" >&2
    echo "retire each one FIRST, then re-run install_client.sh $CLIENT:" >&2
    echo >&2
    for p in "${stale_running[@]:-}" "${stale_ambiguous[@]:-}"; do
      [ -n "$p" ] || continue
      echo "  hermes -p $p gateway stop" >&2
      echo "  hermes -p $p gateway uninstall" >&2
      echo "  hermes profile delete $p" >&2
      echo >&2
    done
    exit 1
  fi
  # Any remaining stale profiles are directories that exist but whose
  # gateway is confirmed NOT running -- safe to proceed, just worth a
  # cleanup reminder at the very end (see the success-path note below).
fi

case "$(_gateway_status)" in
  running)
    GATEWAY_WAS_RUNNING=1
    echo "Stopping the gateway before making any change..."
    HERMES_HOME="$HERMES_HOME" hermes gateway stop
    ;;
  not-running)
    GATEWAY_WAS_RUNNING=0
    ;;
  *)
    echo "ERROR: could not determine whether the default-profile gateway is running" >&2
    echo "('hermes gateway status' failed or returned unrecognized output) —" >&2
    echo "aborting before touching any live file rather than guessing." >&2
    exit 1
    ;;
esac

# Snapshot config.yaml (if it exists) so the fallible `hermes config set`
# sequence below can be rolled back exactly to this state on any failure --
# including the case where this run is the FIRST one ever on this machine
# and config.yaml doesn't exist yet at all: on failure, it is then deleted
# outright (not left half-written), never "restored" from a snapshot that
# was never taken.
CONFIG_EXISTED_BEFORE=0
HERMES_CONFIG_BACKUP=""
if [ -f "$HERMES_CONFIG" ]; then
  CONFIG_EXISTED_BEFORE=1
  HERMES_CONFIG_BACKUP="$HERMES_CONFIG.bak.$(date +%Y%m%d%H%M%S)"
  cp "$HERMES_CONFIG" "$HERMES_CONFIG_BACKUP"
  chmod 600 "$HERMES_CONFIG_BACKUP"
fi

_rollback_hermes_config_and_fail() {
  echo "ERROR: 'hermes config set' failed — rolling back and leaving the gateway STOPPED." >&2
  if [ "$CONFIG_EXISTED_BEFORE" -eq 1 ]; then
    cp "$HERMES_CONFIG_BACKUP" "$HERMES_CONFIG"
    echo "  Restored $HERMES_CONFIG from $HERMES_CONFIG_BACKUP" >&2
  else
    rm -f "$HERMES_CONFIG"
    echo "  Removed $HERMES_CONFIG (it did not exist before this install attempt)" >&2
  fi
  echo "  $ROOT_ENV and $HERMES_ENV were NOT modified at all — both were still" >&2
  echo "  only uncommitted temp files at the point of failure, now discarded." >&2
  echo "  Fix whatever 'hermes config set' failed on, then re-run: install_client.sh $CLIENT" >&2
  if [ "$GATEWAY_WAS_RUNNING" -eq 1 ]; then
    echo "  The gateway was running before this install and is now stopped — start it manually once fixed: hermes gateway start" >&2
  fi
  exit 1
}

# hermes config set operates on the currently-active (sticky) profile —
# force it to "default" first so a stray `hermes profile use <other>` left
# over from earlier session experimentation can't silently redirect these
# writes at the wrong profile. This whole sequence runs BEFORE either
# staged .env file is committed (see the two `mv -f` lines below) — a
# failure at any point here is caught by the config.yaml rollback above,
# and simply discards the staged .env temp files via the EXIT trap without
# ever having touched their live counterparts.
HERMES_HOME="$HERMES_HOME" hermes profile use default || _rollback_hermes_config_and_fail
HERMES_HOME="$HERMES_HOME" hermes config set model.provider "$HERMES_MODEL_PROVIDER" || _rollback_hermes_config_and_fail
HERMES_HOME="$HERMES_HOME" hermes config set model.default "$HERMES_MODEL_DEFAULT" || _rollback_hermes_config_and_fail
HERMES_HOME="$HERMES_HOME" hermes config set skills.external_dirs "[\"$FIELDKIT_ROOT/platform/photo-agent/skills\"]" || _rollback_hermes_config_and_fail

# --- Commit point: every fallible step above has succeeded. From here on,
# only atomic, already-validated renames and a final gateway start remain.
# --- An audit-only backup of the PREVIOUS hermes .env (not used for any
# rollback logic -- by this point config.yaml is already correctly applied
# and there is nothing left to roll back to) is taken purely so a human can
# see what changed. --------------------------------------------------------
if [ -f "$HERMES_ENV" ]; then
  HERMES_ENV_AUDIT_BACKUP="$HERMES_ENV.bak.$(date +%Y%m%d%H%M%S)"
  cp "$HERMES_ENV" "$HERMES_ENV_AUDIT_BACKUP"
  chmod 600 "$HERMES_ENV_AUDIT_BACKUP"
fi

mv -f "$ROOT_ENV_TMP" "$ROOT_ENV"
chmod 600 "$ROOT_ENV"
mv -f "$HERMES_ENV_TMP" "$HERMES_ENV"
chmod 600 "$HERMES_ENV"

if [ "$NO_RESTART" -eq 0 ]; then
  HERMES_HOME="$HERMES_HOME" hermes gateway start
fi

echo "Installed '$CLIENT' as the active client."
echo

if [ "${#stale_not_running[@]}" -gt 0 ]; then
  echo "NOTE: the following old per-client Hermes profile(s) still exist on this"
  echo "machine, but their gateway was confirmed NOT running, so this install"
  echo "proceeded. They are no longer used — this project runs one client at a"
  echo "time via the default profile only (issue #61). Delete them at your"
  echo "convenience (not run by this script — live state):"
  echo
  for p in "${stale_not_running[@]}"; do
    echo "  hermes -p $p gateway uninstall"
    echo "  hermes profile delete $p"
    echo
  done
fi
