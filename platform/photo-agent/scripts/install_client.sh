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
#     in that sequence leaves BOTH .env files completely untouched (the
#     temp files are discarded), and rolls config.yaml back to exactly what
#     it was before this run. TWO more fallible operations remain even
#     after that sequence succeeds, though: the two file renames that
#     actually commit the staged `.env` files. Neither is expected to fail
#     (both are local, same-filesystem renames of already-staged, already-
#     validated files), but this script does not assume it — a snapshot
#     (content AND original file mode) of whatever config.yaml and Hermes's
#     .env looked like before this run is kept available until BOTH renames
#     succeed, and a failure on EITHER one rolls back whichever of the two
#     already changed (config.yaml, and Hermes's .env if it was the first
#     rename that succeeded) to its exact pre-install state — not left as
#     "manual recovery guidance". The root .env is the very last thing
#     committed, and a failed rename leaves its own target untouched by
#     definition (POSIX rename is atomic), so there is nothing to roll back
#     for it specifically. Net result: there is no window where one live
#     file reflects the new client and another still reflects the old one
#     — barring the restore step of a rollback itself failing, an
#     essentially unreachable double-failure this script detects and
#     reports loudly ("MANUAL INTERVENTION REQUIRED") rather than silently
#     accepting.
#   - The client name is validated against a strict allowlist pattern before
#     it ever touches a path, and BOTH the resolved client directory and the
#     resolved client .env FILE (a symlink at either level, not just the
#     directory) are canonicalized and verified to still live under
#     clients/ before anything reads or touches them — no path traversal,
#     no symlink escape at either level.
#   - Gateway status (this profile's, and every stale non-default profile's)
#     is checked BEFORE any live file is touched, using a classifier built
#     from Hermes's own real status-reporting source (not guessed strings)
#     — including reconciling two of Hermes's own INDEPENDENT liveness
#     checks that can disagree in the same real output (see `_gateway_status`
#     below for the full writeup). An AMBIGUOUS status (command failure,
#     unrecognized output) is treated as "assume running" and aborts the
#     whole install — never as "assume stopped" — because proceeding on a
#     wrong guess is exactly the two-gateways-live exposure this script
#     exists to prevent. Any stale, non-default profile confirmed running
#     aborts the install outright, with the exact retirement commands,
#     rather than merely warning about it after the fact once the new
#     gateway is already up — checked once before any mutation, and once
#     more immediately before the new gateway actually starts, closing the
#     race window between those two moments.
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

# NOTE: fixing the client's own source .env's permissions (chmod 600) is
# deliberately NOT done here. An earlier version did it at this point --
# before the fallible `hermes config set` sequence and the stale-profile
# checks below -- which meant a failed install still left one real,
# pre-existing file mutated, contradicting "zero filesystem mutation on
# failure." It's now done only as part of the final commit, alongside the
# ROOT_ENV/HERMES_ENV chmods, after every fallible step has succeeded --
# see the "Commit point" section near the bottom of this script.

# --- Gateway status: FAIL CLOSED on anything ambiguous. A command failure
# or unrecognized output is treated as "assume running" for a stale
# non-default profile (the more dangerous unknown to get wrong is "assumed
# retired when it's actually still live") and as "abort, don't guess" for
# the default profile this script is about to reconfigure — proceeding on
# a wrong guess in either direction is exactly the kind of exposure this
# script exists to prevent, so an ambiguous read is never treated as
# license to proceed. ---------------------------------------------------
#
# The classifier below is tuned to Hermes's REAL `gateway status` output
# contract on macOS (this project's actual deployment target), read
# directly from hermes_cli/gateway.py's `launchd_status()` and
# `_print_gateway_process_mismatch()` — NOT invented/guessed strings. A
# naive "contains the word running" heuristic (this script's own earlier
# version) never matches the single most common real output line at all:
#   ✓ Gateway is supervised by launchd (PID 462)
# — which contains neither "running" nor any negation of it, so the old
# classifier misclassified a genuinely running gateway as "ambiguous" and
# aborted every real install on this machine. Confirmed live (read-only
# query, no live state touched) against both the default profile and
# mercury's still-running leftover profile — both real captures are used
# verbatim as test fixtures in test_install_client.py, not reconstructed.
#
# Full real-output contract (macOS/launchd path, `launchd_status()`):
#   POSITIVE (running) fragments actually printed by Hermes:
#     "is supervised by launchd"            -- the common case (PID N)
#     "is running"                          -- covers every other real
#       phrasing Hermes uses for a confirmed live process: "Detached
#       fallback process is running (PID N)", "Detached gateway process
#       is running (PID N)", "Gateway is running (PID: N)" (no-service-
#       installed-but-a-manual-PID-found case), "Gateway is running as a
#       detached fallback process" and "Gateway process is running for
#       this profile" (the process/service mismatch warnings), "Gateway
#       service is installed and running" (setup wizard's own phrasing,
#       included for robustness even though `gateway status` itself
#       doesn't emit it). None of Hermes's real NEGATIVE phrasings below
#       contain "is running" as a substring (verified directly against
#       the source: "is NOT running" has "not" between "is" and
#       "running", so it does not match) -- confirmed safe to check
#       broadly rather than needing every exact phrase enumerated.
#   NEGATIVE (not running) fragments:
#     "is not running"                      -- catch-all no-service-found case
#     "service is not loaded"               -- launchd hasn't loaded the def
#     "not supervising it"                  -- registered but not supervised,
#                                                AND no detached fallback found
#                                                (if one WAS found, "is running"
#                                                is also printed and wins, since
#                                                POSITIVE is checked first)
#     "No fallback process is running"      -- an EXPLICIT NEGATIVE despite
#                                                containing the substring "is
#                                                running" ("process is
#                                                running" reads as a positive
#                                                fragment on its own).
#     "service is not installed"            -- gateway install was never run
#   Anything else: ambiguous -- fail closed, abort.
#
# THE SUBSTRING-PRECEDENCE BUG (found across two rounds of live testing --
# get this right, it's the crux of the whole classifier):
#   Round 1 fix: check "No fallback process is running" FIRST, before the
#   generic POSITIVE check, so its "is running" substring can't misfire as
#   positive. That fix was itself incomplete: `launchd_status()`'s own
#   fallback-PID check ("No fallback process is running") and
#   `_print_gateway_process_mismatch()`'s INDEPENDENT, broader process scan
#   (`find_gateway_pids()`) are two genuinely different detection
#   mechanisms that can DISAGREE and both print in the SAME real output --
#   reproduced directly against gateway.py's real source:
#     ✗ No fallback process is running
#
#     ⚠ Gateway process is running for this profile, but the service is not active
#       PID(s): 123
#   Checking the negative phrase first and returning immediately (the
#   round-1 fix) made this combined, real, reproducible case return
#   "not-running" even though a live gateway process genuinely exists --
#   fail-OPEN, exactly backwards for this script's purpose.
#
#   Round 2 fix (this one): the negative phrase is stripped out of a COPY
#   of the output BEFORE the generic POSITIVE check runs, rather than
#   short-circuiting on it -- so any OTHER, independent positive evidence
#   elsewhere in the output (the mismatch warning, a PID, "is supervised by
#   launchd", etc.) is still found and wins, while the specific phrase that
#   would otherwise false-positive on its own "is running" substring is
#   neutralized either way. Positive evidence is checked FIRST on the
#   stripped copy; only when NONE exists anywhere does the (unstripped)
#   negative-phrase check run. See
#   test_classifier_recognizes_real_hermes_process_service_mismatch_as_running
#   in test_install_client.py for the fixture proving this, built directly
#   from gateway.py's real _print_gateway_process_mismatch() source.
#
# _gateway_status [extra hermes args...] -- echoes one of:
#   running | not-running | ambiguous
# Never mutates anything -- pure query.
_gateway_status() {
  local out rc stripped
  out="$(HERMES_HOME="$HERMES_HOME" hermes "$@" gateway status 2>&1)"; rc=$?
  if [ $rc -ne 0 ]; then
    echo "ambiguous"; return
  fi
  # Neutralize (not short-circuit on) the one negative phrase that
  # contains "is running" as a literal substring, so it can never
  # masquerade as positive evidence -- but without discarding genuine,
  # independent positive evidence that can legitimately appear alongside
  # it in the same real output (see the long comment above).
  stripped="$(printf '%s' "$out" | sed -E 's/No fallback process is running//g')"
  if printf '%s' "$stripped" | grep -qE 'is supervised by launchd|is running'; then
    echo "running"; return
  fi
  if printf '%s' "$out" | grep -qE 'is not running|service is not loaded|not supervising it|service is not installed|No fallback process is running'; then
    echo "not-running"; return
  fi
  echo "ambiguous"
}

# --- Stale non-default Hermes profiles (e.g. ~/.hermes/profiles/mercury from
# before issue #61): checked and, if any is confirmed running, this install
# is ABORTED — before touching anything live — rather than merely warned
# about after the fact once the new default-profile gateway is already up.
# A leftover per-client-profile gateway is a fully separate, independently-
# running launchd service this script never starts or stops in any other
# code path; if it's still live, it stays reachable on its own Telegram bot
# with its own client's credentials regardless of what this script does to
# the default profile, so the two-gateways-live exposure this script exists
# to prevent can only actually be prevented by refusing to proceed until
# it's retired. See platform/docs/hermes/09-per-client-model-profiles.md
# for the full writeup and the exact retirement commands (printed below
# too).
#
# Called TWICE: once here, before ANY mutation of any kind (mkdir, chmod,
# lock, temp file) -- not merely before the live .env files, as an earlier
# version did, which left a real window where a stale profile's gateway
# could still be confirmed live only after this script had already created
# directories, taken the lock, and staged secrets -- and once again,
# immediately before the final `hermes gateway start` call near the bottom
# of this script, to close the TOCTOU gap where a stale profile's gateway
# could start in the interval between the first check and this script
# actually bringing up the new one. Populates (overwriting on each call)
# the global stale_running / stale_not_running / stale_ambiguous arrays;
# the LATER call's stale_not_running is what the final success-path cleanup
# reminder uses, since it's the freshest snapshot. Declared unconditionally
# so referencing their length later under `set -u` never hits an
# unbound-variable error on a bash without the 4.4+ fix for `"${arr[@]}"`
# on an empty array (this machine's stock /bin/bash is 3.2.57, which has
# that exact bug — confirmed via direct testing).
stale_running=()
stale_not_running=()
stale_ambiguous=()

# _contains NEEDLE ARG... -- O(n) membership test. Not a bash-4+
# associative array (bash 3.2, this machine's stock /bin/bash, has no
# `declare -A`) -- the stale-profile candidate lists this dedupes are
# always tiny (realistically 0-2 entries), so O(n^2) is irrelevant here.
_contains() {
  local needle="$1" x
  shift
  for x in "$@"; do
    [ "$x" = "$needle" ] && return 0
  done
  return 1
}

# _launchctl_gateway_candidates -- echoes, one per line, the profile-name
# portion of every LOADED `ai.hermes.gateway-<name>` launchd service,
# enumerated DIRECTLY via `launchctl list` -- independent of whether
# `$HERMES_HOME/profiles/<name>/` still exists as a directory. This is
# what catches an ORPHANED service: a profile directory that was deleted
# or renamed, whose launchd service definition is nonetheless still loaded
# and potentially alive with credentials in memory -- a scenario Hermes's
# own source acknowledges renamed/orphan profiles create. The bare
# `ai.hermes.gateway` label (the DEFAULT profile -- the one this script
# itself manages) is deliberately excluded; it is never "stale".
#
# TWO distinct "nothing found" cases, handled differently -- do not
# conflate them:
#   1. `launchctl` isn't on PATH at all (non-macOS). Legitimately not an
#      error: orphan detection is additive to the directory scan on this
#      platform, never a hard requirement of it. Yields nothing, sets no
#      failure flag.
#   2. `launchctl` IS on PATH but `launchctl list` itself FAILS (nonzero
#      exit -- a permission issue, launchd transiently unavailable, etc.).
#      This is NOT "no services found" -- it's "the answer is unknown",
#      and a prior version of this function treated the two identically
#      (silently returning empty either way), which is fail-OPEN: an
#      orphaned gateway with no profile directory would go completely
#      undetected and the install would proceed right underneath it,
#      directly contradicting this script's own "running or unconfirmable
#      aborts" policy applied everywhere else. Communicated to the caller
#      via this function's own exit status (1 = enumeration failed, 0 =
#      succeeded, possibly with zero candidates) -- NOT a global variable
#      mutated from inside the function, because the caller consumes this
#      function's output via `<(...)` process substitution or `$(...)`
#      command substitution, BOTH of which run the function body in a
#      SUBSHELL in bash; a variable assignment made inside that subshell
#      is invisible to the parent shell once the subshell exits. This was
#      caught and fixed during this round's own implementation, before it
#      ever shipped as a real bug: an earlier draft set a global flag
#      inside this function and checked it in the caller, which silently
#      never worked for exactly this reason.
_launchctl_gateway_candidates() {
  command -v launchctl >/dev/null 2>&1 || return 0
  local out rc
  out="$(launchctl list 2>&1)"; rc=$?
  if [ $rc -ne 0 ]; then
    return 1
  fi
  printf '%s\n' "$out" \
    | awk '$3 ~ /^ai\.hermes\.gateway-/ {print $3}' \
    | sed -E 's/^ai\.hermes\.gateway-//'
  return 0
}

# _launchctl_label_has_live_pid LABEL -- "running" if `launchctl list
# LABEL` shows a numeric PID (a live, launchd-confirmed process), else
# "not-running" (label not loaded at all, or loaded with no PID). Used as
# a direct, independent aliveness signal for orphaned services -- `hermes
# -p <name> gateway status` may itself behave unpredictably for a profile
# whose directory no longer exists, so this doesn't rely on it for the
# specifically-orphaned case.
_launchctl_label_has_live_pid() {
  local label="$1" line
  line="$(launchctl list "$label" 2>/dev/null | grep '"PID"')" || true
  if printf '%s' "$line" | grep -qE '[0-9]'; then
    echo "running"
  else
    echo "not-running"
  fi
}

# _abort_if_stale_profiles_running early|late -- the message differs by
# call site: the EARLY call (before any mutation) can truthfully say
# nothing has happened yet; the LATE call (right before `gateway start`,
# after both .env files are already committed) must NOT claim that, since
# claiming "refusing to install" at that point would be actively
# misleading -- the install (the file switch) already succeeded; only
# starting the new gateway is being withheld.
_abort_if_stale_profiles_running() {
  local stage="$1"
  stale_running=()
  stale_not_running=()
  stale_ambiguous=()
  local d p candidates=()
  if [ -d "$HERMES_HOME/profiles" ]; then
    for d in "$HERMES_HOME"/profiles/*/; do
      [ -d "$d" ] || continue
      p="$(basename "$d")"
      _contains "$p" "${candidates[@]:-}" || candidates+=("$p")
    done
  fi
  # Plain command substitution (not `< <(...)` process substitution) so
  # the exit status is capturable at all -- process substitution's exit
  # status is not visible to the enclosing command in bash, which is
  # exactly why an earlier draft's failure-signaling attempt (a global
  # variable mutated from inside the function) silently never worked (see
  # that function's own comment for the full story).
  #
  # TWO more traps avoided here, both caught live while implementing this:
  #   1. `local launchctl_raw=$(...)` (local + assignment on ONE line) is
  #      a well-known bash gotcha where `$?` afterward reflects the
  #      `local` builtin's own (always-0) exit status, not the command
  #      substitution's -- `local` is declared on its own line instead.
  #   2. Even with that split, `launchctl_raw="$(_launchctl_gateway_candidates)"`
  #      as a bare assignment is a "simple command" whose own exit status
  #      becomes the command substitution's exit status -- under this
  #      script's `set -e`, a bare assignment like that failing aborts the
  #      script IMMEDIATELY at that line, before the `if` below checking
  #      $launchctl_rc ever runs at all (this was reproduced live: the
  #      intended error message never printed, the script just silently
  #      exited). Fixed with the standard idiom below: appending
  #      `|| launchctl_rc=$?` makes the WHOLE statement's own exit status
  #      always 0 (so `set -e` never fires here), while still capturing
  #      the real failure code into launchctl_rc when the left side fails.
  local launchctl_raw launchctl_rc=0
  launchctl_raw="$(_launchctl_gateway_candidates)" || launchctl_rc=$?
  if [ "$launchctl_rc" -ne 0 ]; then
    echo "ERROR: 'launchctl list' failed — cannot confirm whether an orphaned," >&2
    echo "non-default Hermes gateway service (one with no matching profile" >&2
    echo "directory under $HERMES_HOME/profiles/, or any other) is currently" >&2
    echo "loaded and running. Refusing to proceed on an unconfirmable answer —" >&2
    echo "the same fail-closed policy this script applies to every other" >&2
    echo "gateway-status check." >&2
    if [ "$stage" = "late" ]; then
      echo "$ROOT_ENV and $HERMES_ENV were ALREADY switched to '$CLIENT' above and are" >&2
      echo "NOT being reverted -- only starting the new gateway is being refused." >&2
    fi
    exit 1
  fi
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    _contains "$p" "${candidates[@]:-}" || candidates+=("$p")
  done <<< "$launchctl_raw"

  for p in "${candidates[@]:-}"; do
    [ -n "$p" ] || continue
    # A launchctl-confirmed live PID for this profile's label is direct,
    # independent evidence -- it wins outright regardless of what `hermes
    # -p <name> gateway status` says (which may itself be unreliable for
    # an orphaned profile with no directory). Only when launchctl shows no
    # live PID for this label (or has no opinion, e.g. not macOS) does the
    # existing CLI-based check apply, exactly as before.
    if [ "$(_launchctl_label_has_live_pid "ai.hermes.gateway-$p")" = "running" ]; then
      stale_running+=("$p")
      continue
    fi
    case "$(_gateway_status -p "$p")" in
      running) stale_running+=("$p") ;;
      not-running) stale_not_running+=("$p") ;;
      *) stale_ambiguous+=("$p") ;;
    esac
  done
  if [ "${#stale_running[@]}" -eq 0 ] && [ "${#stale_ambiguous[@]}" -eq 0 ]; then
    # Any remaining stale profiles are directories that exist but whose
    # gateway is confirmed NOT running -- safe to proceed, just worth a
    # cleanup reminder at the very end (see the success-path note below).
    return 0
  fi
  if [ "$stage" = "late" ]; then
    echo "ERROR: a stale, non-default Hermes profile started running between this" >&2
    echo "script's initial check and now (a race, not a bug in the check itself)." >&2
    echo "$ROOT_ENV and $HERMES_ENV were ALREADY switched to '$CLIENT' above and are" >&2
    echo "NOT being reverted -- they're correct for '$CLIENT'. What's being refused" >&2
    echo "is starting the new default-profile gateway, which would otherwise create" >&2
    echo "the exact two-gateways-live exposure issue #59 was about, right now:" >&2
  else
    echo "ERROR: refusing to install '$CLIENT' — at least one stale, non-default" >&2
    echo "Hermes profile still has a gateway that is running (or whose status" >&2
    echo "could not be confirmed, which this script treats the same way):" >&2
  fi
  for p in "${stale_running[@]:-}"; do [ -n "$p" ] && echo "  - $p (confirmed RUNNING)" >&2; done
  for p in "${stale_ambiguous[@]:-}"; do [ -n "$p" ] && echo "  - $p (status could not be confirmed)" >&2; done
  echo >&2
  if [ "$stage" = "late" ]; then
    echo "Retire it, then start the (already-switched) gateway yourself:" >&2
  else
    echo "This is exactly the two-gateways-live exposure issue #59 was about —" >&2
    echo "retire each one FIRST, then re-run install_client.sh $CLIENT:" >&2
  fi
  echo >&2
  for p in "${stale_running[@]:-}" "${stale_ambiguous[@]:-}"; do
    [ -n "$p" ] || continue
    echo "  hermes -p $p gateway stop" >&2
    echo "  hermes -p $p gateway uninstall" >&2
    echo "  hermes profile delete $p" >&2
    echo >&2
  done
  if [ "$stage" = "late" ]; then
    echo "  HERMES_HOME=\"$HERMES_HOME\" hermes gateway start" >&2
  fi
  exit 1
}

_abort_if_stale_profiles_running early

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

# Default-profile gateway status check -- both _gateway_status and
# _abort_if_stale_profiles_running are already defined above, before any
# mutation. This is the DEFAULT profile's own status (not a stale
# non-default one), checked here, right before the fallible config-set
# sequence, so it can be stopped first if running.
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

# Portable file-mode capture (macOS's BSD `stat` and Linux's GNU `stat`
# take incompatible flags for this -- `%OLp` vs `%a` -- so, consistent with
# `_realpath` above, this uses python3, already a hard dependency of every
# sibling script in this repo). Returns a plain octal string like "640",
# suitable for `chmod` directly.
_file_mode() {
  python3 -c 'import os, stat, sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode))[2:])' "$1"
}

# Snapshot config.yaml -- content AND mode -- (if it exists) so the fallible
# `hermes config set` sequence below can be rolled back exactly to this
# state on any failure, including the case where this run is the FIRST one
# ever on this machine and config.yaml doesn't exist yet at all: on
# failure, it is then deleted outright (not left half-written), never
# "restored" from a snapshot that was never taken.
#
# The mode is captured and explicitly re-applied on rollback, not left to
# `cp`'s own default behavior -- a real gap found live while testing this
# fix: `hermes config set` can rewrite config.yaml via its own atomic
# write (a fresh inode with a fresh, more permissive default mode), so by
# the time a LATER `hermes config set` call in this same sequence fails,
# the live config.yaml's mode may already differ from what it was before
# this script ever touched it. `cp` alone preserves whatever mode the
# CURRENT (already-rewritten-by-hermes) destination inode has, not the
# ORIGINAL mode from before this run -- confirmed live: a 0640 original
# became 0666 after a simulated failure, using `cp` alone for restore.
CONFIG_EXISTED_BEFORE=0
HERMES_CONFIG_BACKUP=""
HERMES_CONFIG_ORIGINAL_MODE=""
if [ -f "$HERMES_CONFIG" ]; then
  CONFIG_EXISTED_BEFORE=1
  HERMES_CONFIG_ORIGINAL_MODE="$(_file_mode "$HERMES_CONFIG")"
  HERMES_CONFIG_BACKUP="$HERMES_CONFIG.bak.$(date +%Y%m%d%H%M%S)"
  cp "$HERMES_CONFIG" "$HERMES_CONFIG_BACKUP"
  chmod 600 "$HERMES_CONFIG_BACKUP"
fi

# Snapshot Hermes's .env -- content AND mode -- for the exact same reason
# and at the exact same point as config.yaml just above: BEFORE the
# fallible `hermes config set` sequence runs, not after it succeeds. A
# prior version took this snapshot AFTER that sequence (right before the
# file-commit renames instead), which is itself an unguarded, fallible
# `cp`/`chmod` pair running under `set -e` with config.yaml ALREADY
# applied by that point -- if the snapshot itself failed there, the script
# would exit with config.yaml switched to the new client and no rollback
# at all. Taking it here instead means a failure capturing this snapshot
# happens before ANYTHING is mutated (the config-set sequence hasn't run
# yet), so `set -e`'s plain abort is exactly the right, safe behavior for
# it -- no rollback machinery is needed for a failure at this point.
HERMES_ENV_EXISTED_BEFORE=0
HERMES_ENV_BACKUP=""
HERMES_ENV_ORIGINAL_MODE=""
if [ -f "$HERMES_ENV" ]; then
  HERMES_ENV_EXISTED_BEFORE=1
  HERMES_ENV_ORIGINAL_MODE="$(_file_mode "$HERMES_ENV")"
  HERMES_ENV_BACKUP="$HERMES_ENV.bak.$(date +%Y%m%d%H%M%S)"
  cp "$HERMES_ENV" "$HERMES_ENV_BACKUP"
  chmod 600 "$HERMES_ENV_BACKUP"
fi

# Restores config.yaml to exactly its pre-install snapshot (content AND
# mode), or deletes it if it didn't exist before this install attempt.
# Shared by every failure path from this point on in the script: a
# config-set failure, AND either of the two final file-commit failures
# further below -- all three mean config.yaml (already applied to the new
# client by the point any of them can happen) must not be left switched
# while other live state stays on the old client. This is what closes the
# "config.yaml already applied while both .env files are still old"
# inconsistency a prior version of this script's error message implicitly
# claimed couldn't happen.
#
# Reports its OWN actual outcome via its own echo statements (never
# "Restored" unless the restore genuinely succeeded) and returns 1 on
# failure -- a prior version's CALLERS printed "Restored $FILE" as an
# assumed fact regardless of whether the cp/chmod inside actually
# succeeded, which could itself lie about the true post-failure state.
# Callers invoke this as `_restore_config_yaml || true` (not letting its
# own nonzero return trip `set -e` early) so the script still reaches its
# trailing "fix this and re-run" instructions even when the restore itself
# failed -- the WARNING line already told the operator MANUAL INTERVENTION
# is required at that point, which is a stronger signal than a truncated
# script anyway. ------------------------------------------------------------
_restore_config_yaml() {
  if [ "$CONFIG_EXISTED_BEFORE" -eq 1 ]; then
    if cp "$HERMES_CONFIG_BACKUP" "$HERMES_CONFIG" && chmod "$HERMES_CONFIG_ORIGINAL_MODE" "$HERMES_CONFIG"; then
      echo "  Restored $HERMES_CONFIG from $HERMES_CONFIG_BACKUP (mode $HERMES_CONFIG_ORIGINAL_MODE)" >&2
    else
      echo "  WARNING: restoring $HERMES_CONFIG from $HERMES_CONFIG_BACKUP FAILED -- MANUAL INTERVENTION REQUIRED." >&2
      return 1
    fi
  else
    if rm -f "$HERMES_CONFIG"; then
      echo "  Removed $HERMES_CONFIG (it did not exist before this install attempt)" >&2
    else
      echo "  WARNING: removing $HERMES_CONFIG FAILED -- MANUAL INTERVENTION REQUIRED." >&2
      return 1
    fi
  fi
}

# Same contract as _restore_config_yaml above, for Hermes's .env.
_restore_hermes_env() {
  if [ "$HERMES_ENV_EXISTED_BEFORE" -eq 1 ]; then
    if cp "$HERMES_ENV_BACKUP" "$HERMES_ENV" && chmod "$HERMES_ENV_ORIGINAL_MODE" "$HERMES_ENV"; then
      echo "  Restored $HERMES_ENV from $HERMES_ENV_BACKUP (mode $HERMES_ENV_ORIGINAL_MODE)" >&2
    else
      echo "  WARNING: restoring $HERMES_ENV from $HERMES_ENV_BACKUP FAILED -- MANUAL INTERVENTION REQUIRED." >&2
      return 1
    fi
  else
    if rm -f "$HERMES_ENV"; then
      echo "  Removed $HERMES_ENV (it did not exist before this install attempt)" >&2
    else
      echo "  WARNING: removing $HERMES_ENV FAILED -- MANUAL INTERVENTION REQUIRED." >&2
      return 1
    fi
  fi
}

_rollback_hermes_config_and_fail() {
  echo "ERROR: 'hermes config set' failed — rolling back and leaving the gateway STOPPED." >&2
  _restore_config_yaml || true
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

# --- Commit point: every fallible step above has succeeded, and both
# config.yaml's and Hermes .env's pre-install snapshots (content + mode)
# were ALREADY captured earlier, before the `hermes config set` sequence
# ran (see the comment there) -- specifically so nothing fallible remains
# unguarded between "config.yaml gets applied" and "a rollback path exists
# for it". Two fallible operations remain here: the two file renames
# below. Neither is expected to fail -- both are local, same-filesystem
# renames of already-validated, already-staged temp files, about as
# reliable an operation as this script performs -- but "not expected to
# fail" is not "provably cannot fail", and a prior version of this script
# treated it as the latter in two ways: it printed "re-run to fix it"
# guidance on a second-rename failure rather than actually rolling
# anything back, and its post-rename `chmod 600` calls (removed below —
# see next paragraph) were themselves unguarded fallible operations that
# could exit the script via bare `set -e` with config.yaml and/or Hermes's
# .env already switched and no rollback triggered at all. -----------------
#
# The temp files staged earlier (chmod 600 "$ROOT_ENV_TMP" "$HERMES_ENV_TMP",
# see the staging section above) are ALREADY mode 600 before either rename
# below runs -- `mv`/rename(2) never changes a file's mode, only its
# directory entry, confirmed directly (`mv -f` a 0600 file onto a new
# name, the destination is still 0600). The post-rename `chmod 600` calls
# a prior version had here were therefore both REDUNDANT and themselves an
# unguarded failure point -- removed entirely rather than guarded, since
# the correct fix for a redundant, avoidable mutation is to not perform it.

# The client's own source .env's permissions are fixed here, as the first
# real mutation of the commit phase -- deferred from validation time (see
# the NOTE near the top of this script) so a failure anywhere before this
# point leaves it, like every other live file, completely untouched. Not
# part of the config.yaml/.env-files rollback set below: chmod 600 is
# strictly permission-TIGHTENING and idempotent, nothing meaningful to
# roll back regardless of whether the rest of this install succeeds.
chmod 600 "$CLIENT_ENV" 2>/dev/null || true

# --- Two-file commit ordering, and why: Hermes's own .env is committed
# FIRST, not root .env. Hermes's .env is the file that actually governs
# LIVE SKILL DISPATCH (see the module docstring's "Why CLIENT_NAME is also
# written into Hermes's own .env" section) -- the exact path issue #59 was
# about. If the SECOND rename below fails, this ordering plus the rollback
# logic means either outcome is fully consistent: Hermes's .env (rolled
# back along with config.yaml) ends up matching root .env's untouched old
# state, never a mix. Both `_restore_*` calls below are followed by
# `|| true` so their own nonzero return (on a restore itself failing --
# see their definitions above for why that's reported, not assumed away)
# doesn't trip `set -e` before this block's own trailing diagnostic lines
# have a chance to print. --------------------------------------------------
mv -f "$HERMES_ENV_TMP" "$HERMES_ENV" || {
  echo "ERROR: failed to commit $HERMES_ENV -- rolling back config.yaml too (it was already applied by the successful 'hermes config set' sequence above), so nothing is left half-switched." >&2
  _restore_config_yaml || true
  echo "  Neither $HERMES_ENV nor $ROOT_ENV was modified. Re-run: install_client.sh $CLIENT" >&2
  exit 1
}

mv -f "$ROOT_ENV_TMP" "$ROOT_ENV" || {
  echo "ERROR: failed to commit $ROOT_ENV after $HERMES_ENV already succeeded -- rolling BOTH $HERMES_ENV and config.yaml back to their pre-install state, so nothing is left half-switched." >&2
  _restore_hermes_env || true
  _restore_config_yaml || true
  echo "  $ROOT_ENV was never modified (a failed rename leaves its target untouched, unlike a failed copy). Re-run: install_client.sh $CLIENT" >&2
  exit 1
}

if [ "$NO_RESTART" -eq 0 ]; then
  # TOCTOU recheck (issue #62 review Security-5b): re-verify no stale
  # non-default profile has started running in the window between the
  # FIRST check (before any mutation, at the very top of this script) and
  # this, the last possible moment before the new default-profile gateway
  # actually comes up. Both live .env files are already correctly
  # committed above regardless of this recheck's outcome -- if it now
  # finds a stale profile running, the switch itself is NOT undone (that
  # would reintroduce its own inconsistency, and the files are correct for
  # the client being installed), but the NEW gateway is refused a start,
  # so this script never itself creates a two-gateways-live moment.
  _abort_if_stale_profiles_running late
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
