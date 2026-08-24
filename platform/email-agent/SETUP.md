# Email Agent — Mac Mini Setup

One-time setup procedure for a new Mac Mini. Run every step in order.
Estimated time: 25–30 minutes.

---

## Architecture overview

```
Telegram /check_email
       │
       ▼
Hermes gateway (LLM)
       │  discovers skills/check-email/SKILL.md via
       │  skills.external_dirs (no copy step, no stale cache)
       │  runs exactly one bash command:
       ▼
python3 scripts/check_email.py
       │
       ├── polls Gmail via gws
       ├── enforces ADMIN_ALLOWLIST
       ├── assigns ref IDs, applies fk-received label
       └── sends Telegram acks via a direct Telegram Bot API call
```

> As of #25, the manual `/check_email` skill is a Hermes-native skill at
> `platform/email-agent/skills/check-email/SKILL.md` — see
> `platform/docs/hermes/08-check-email-skill.md` for the full port writeup.
> `check_email.py`'s cron path and its Telegram notifications have not
> depended on the openclaw binary since #24; this closes the last remaining
> OpenClaw-shaped surface in email-agent.

The script is deterministic — no LLM involvement beyond dispatching the single
bash command. All configuration lives in `.env`. Logs go to `~/src/fieldkit/logs/`,
state to `~/src/fieldkit/data/email-agent/`.

---

## 1 — Homebrew

```bash
brew --version
```

If not found:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

---

## 2 — Google Cloud CLI (`gcloud`)

Required by `gws auth setup` to create the OAuth client.

```bash
# Download (ARM Mac)
curl -O https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-darwin-arm.tar.gz
curl -O https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-darwin-arm.tar.gz.sha256

# Verify checksum before extracting
shasum -a 256 -c google-cloud-cli-darwin-arm.tar.gz.sha256
# Must print: google-cloud-cli-darwin-arm.tar.gz: OK

# Extract
tar -xf google-cloud-cli-darwin-arm.tar.gz

# Install — answer Y to all prompts
./google-cloud-sdk/install.sh

# Open a new terminal, then verify
gcloud --version
```

> If you're on an Intel Mac, replace `darwin-arm` with `darwin-x86_64` in both filenames.

---

## 3 — `gws` CLI

```bash
brew install googleworkspace-cli
gws --version
```

---

## 4 — Configure the OAuth client

`gws` needs a Google Cloud OAuth client before it can authenticate.

```bash
gws auth setup
```

Follow the prompts — it opens Google Cloud Console in the browser and walks you through:
- Creating a GCP project
- Enabling the Gmail API
- Creating an OAuth 2.0 client ID

---

## 5 — Populate `.env`

```bash
cp ~/src/fieldkit/platform/email-agent/.env.example \
   ~/src/fieldkit/platform/email-agent/.env
chmod 600 ~/src/fieldkit/platform/email-agent/.env
```

Edit `.env` and fill in all variables:

| Variable | Value |
|----------|-------|
| `AGENT_EMAIL` | The dedicated agent Gmail address (the one you will authenticate in Step 6) |
| `ADMIN_ALLOWLIST` | Comma-separated permitted sender addresses. **The first address is also the stale-alert email recipient.** |
| `ADMIN_TELEGRAM_CHAT_ID` | Your Telegram chat ID (see note below) |
| `TELEGRAM_BOT_TOKEN` | Bot token for direct Telegram Bot API calls — same token Hermes's gateway uses, see `~/.hermes/.env` |
| `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND` | Set to `file` — required for cron (macOS keychain is not accessible without a user session) |

**Finding `ADMIN_TELEGRAM_CHAT_ID`:** send any message to your bot in Telegram, then run:

```bash
grep "sendMessage ok" ~/.hermes/logs/gateway.log | tail -5
```

The chat ID appears after `chat=`.

---

## 6 — Authenticate against the agent Gmail account

```bash
GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file gws auth login --services gmail
```

A browser window opens. **Sign in with the agent Gmail account** (`$AGENT_EMAIL`), not your personal account.

> The `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file` prefix is required at login time so gws
> stores credentials in a plain file rather than the macOS keychain. The keychain is
> inaccessible from cron (no user session), which would cause auth failures at runtime.

---

## 7 — Create runtime directories

```bash
mkdir -p ~/src/fieldkit/data/email-agent ~/src/fieldkit/logs
```

---

## 8 — Install the skill

Hermes discovers this skill directly from the fieldkit repo via
`skills.external_dirs` in `~/.hermes/config.yaml` — no copy step, no stale
cache. Add `platform/email-agent/skills` alongside the existing
`platform/photo-agent/skills` entry (this is a **new** directory the config
does not list yet, even if `process-photos`/`check-approval` are already
installed):

```yaml
skills:
  external_dirs:
    - ~/src/fieldkit/platform/photo-agent/skills
    - ~/src/fieldkit/platform/email-agent/skills
```

Restart the gateway to pick it up:

```bash
hermes gateway restart
```

> Any time `SKILL.md` changes in the repo, `hermes gateway restart` is
> enough to pick it up — no copy step, unlike OpenClaw's old
> `~/.openclaw/workspace/skills/` flow.

---

## 9 — Verify end-to-end

Run each check in order. All must pass before registering the cron job.

```bash
# gws can reach Gmail
source ~/src/fieldkit/platform/email-agent/.env
gws gmail users messages list --params '{"userId": "me", "q": "is:unread"}'
# Must return JSON (empty messages list is fine)

# Runtime directories exist
ls ~/src/fieldkit/data/email-agent ~/src/fieldkit/logs

# Skill is registered and ready
hermes skills list --source local | grep check-email
# Must show check-email, source local, status enabled

# Script runs without error (dry run — safe to run with no unread mail)
cd ~/src/fieldkit/platform/email-agent
python3 scripts/check_email.py --source cron
# Must exit cleanly with no Python traceback. If inbox is empty, no output expected.
```

Then send `/check_email` in Telegram and confirm you receive either:
- A `✓ Email received` ack for each unread email from the allowlist, or
- `No new emails.` if the inbox is clean

---

## 10 — Register the cron job (T07)

Once all Step 9 checks pass, add a system cron entry to poll Gmail automatically.
The `--source cron` flag suppresses the "No new emails." reply on silent runs.

```bash
PYTHON3=$(which python3)
crontab -l 2>/dev/null | grep -v check_email > /tmp/mycron
cat >> /tmp/mycron << EOF
*/5 * * * * env PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin bash -c 'date && ${PYTHON3} ${HOME}/src/fieldkit/platform/email-agent/scripts/check_email.py --source cron' >> ${HOME}/src/fieldkit/logs/cron.log 2>&1
EOF
crontab /tmp/mycron && rm /tmp/mycron
crontab -l | grep check_email
```

> **Run this command as the user who will own the cron job** (typically your regular account, not root). The unquoted heredoc expands `$HOME` and `$PYTHON3` in your shell — if run via `sudo`, `$HOME` resolves to `/root` and the paths will be wrong.
> Change `*/5` to `*/N` to adjust the polling interval. To update the interval later, remove the entry (`crontab -e`) and re-run this step.
> `PYTHON3=$(which python3)` is baked in at registration time to avoid selecting the wrong interpreter on a machine with multiple Python versions.
> `date` prepends a timestamp to every entry in `cron.log` so you can see when each run fired.

> `env PATH=…` is required because cron does not source your shell profile, and
> `PATH=value cmd` only applies to that one command — subprocesses (like `gws`)
> would revert to cron's minimal PATH. `env` sets the PATH for `python3` and all
> processes it spawns.
> `gws` lives in `/opt/homebrew/bin` (Apple Silicon) or `/usr/local/bin` (Intel).
> `$HOME` and `$PYTHON3` are expanded by your shell when you run this command,
> so the crontab stores the literal values.
> As of Part 1 of #14, `check_email.py` no longer shells out to `openclaw` — the
> cron PATH only needs to resolve `gws` and `python3`.

Verify it was registered:

```bash
crontab -l | grep check_email
```

To confirm the cron path is correct end-to-end, wait up to 5 minutes for the first
automatic run and check the cron log:

```bash
tail -f ~/src/fieldkit/logs/cron.log
```

---

## Troubleshooting

| Error | Fix |
|-------|-----|
| `gws: command not found` | Run Step 3 |
| `gcloud CLI not found` | Run Step 2, open a new terminal after install |
| `No OAuth client configured` | Run Step 4 |
| `gws auth login` fails | Re-run Step 5 — ensure you sign in with `$AGENT_EMAIL` |
| `check-email` missing required binary | `prerequisites.commands` lists `gws` and `python3` — confirm both are on PATH. Run `which gws python3` to verify. |
| `check-email` does not appear in `hermes skills list --source local` | The `skills.external_dirs` entry for `platform/email-agent/skills` is missing from `~/.hermes/config.yaml`, or the gateway hasn't picked it up yet. Re-check Step 8 and restart the gateway. |
| LLM improvises instead of running the script | The skill wasn't discovered. Confirm `platform/email-agent/skills/check-email/SKILL.md` exists and `~/.hermes/config.yaml`'s `skills.external_dirs` includes its parent directory, then run `hermes gateway restart`. |
| `check_email: ADMIN_ALLOWLIST is empty` in Telegram | `.env` is missing or `ADMIN_ALLOWLIST` is blank. Check Step 7. |
| `check_email: ADMIN_TELEGRAM_CHAT_ID is not set` | `.env` is missing `ADMIN_TELEGRAM_CHAT_ID`. Check Step 7. |
| `gws gmail … failed` in Telegram | gws token may have expired. Re-run Step 6. |
| `OS keyring failed` or `Decryption failed` in Telegram | gws was authenticated with the macOS keychain, which is inaccessible from cron. Run `gws auth logout` then re-run Step 6 (with `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file`). Ensure `.env` contains `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file`. |
| `gws binary not found` in cron.log | gws is not on cron PATH. Remove the crontab entry (`crontab -e`) and re-run Step 10. |
| `check_email: AGENT_EMAIL is not set` in Telegram | `.env` is missing `AGENT_EMAIL`. Check Step 5. |
| `RuntimeError: TELEGRAM_BOT_TOKEN is not set` | `.env` is missing `TELEGRAM_BOT_TOKEN`. Check Step 5. |
| Script exits with lock error | Check if another instance is still running: `pgrep -af check_email.py`. If a process is found, wait for it to finish. If no process is found but the error persists, the lock file is stale — delete it: `rm ~/src/fieldkit/data/email-agent/run.lock`. |
