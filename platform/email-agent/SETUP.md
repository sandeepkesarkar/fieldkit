# Email Agent — Mac Mini Setup

One-time setup procedure for a new Mac Mini. Run every step in order.
Estimated time: 25–30 minutes.

---

## Architecture overview

```
Telegram /check_email
       │
       ▼
OpenClaw agent (LLM)
       │  reads SKILL.md body from workspace-native path
       │  runs exactly one bash command:
       ▼
python3 scripts/check_email.py
       │
       ├── polls Gmail via gws
       ├── enforces ADMIN_ALLOWLIST
       ├── assigns ref IDs, applies fk-received label
       └── sends Telegram acks via openclaw message send
```

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

# Extract
tar -xf google-cloud-cli-darwin-arm.tar.gz

# Install — answer Y to all prompts
./google-cloud-sdk/install.sh

# Open a new terminal, then verify
gcloud --version
```

> If you're on an Intel Mac, replace `darwin-arm` with `darwin-x86_64` in the filename.

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
| `ADMIN_ALLOWLIST` | Comma-separated permitted sender addresses |
| `POLLING_INTERVAL_MINUTES` | How often to poll (e.g. `5`) |
| `ADMIN_TELEGRAM_CHAT_ID` | Your Telegram chat ID (see note below) |
| `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND` | Set to `file` — required for cron (macOS keychain is not accessible without a user session) |

**Finding `ADMIN_TELEGRAM_CHAT_ID`:** send any message to your OpenClaw bot in Telegram, then run:

```bash
grep "sendMessage ok" ~/.openclaw/logs/gateway.log | tail -5
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

The SKILL.md must be placed directly in the OpenClaw workspace — this is what causes
the skill body to be injected into the agent session. The `extraDirs` config approach
lists the skill but does not inject the body (OpenClaw issue #65946).

```bash
mkdir -p ~/.openclaw/workspace/skills/check_email
cp ~/src/fieldkit/platform/email-agent/SKILL.md \
   ~/.openclaw/workspace/skills/check_email/SKILL.md
```

Restart the gateway to load the new skill:

```bash
openclaw gateway restart
```

> **Important:** any time SKILL.md changes in the repo, re-run the `cp` command and
> restart the gateway. The workspace copy is not auto-synced from the repo.

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
openclaw skills list | grep check_email
# Must show: ✓ ready  📦 check_email  …  openclaw-workspace

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
`POLLING_INTERVAL_MINUTES` from `.env` controls the schedule.

```bash
OPENCLAW_BIN=$(dirname $(which openclaw))
crontab -l 2>/dev/null | grep -v check_email > /tmp/mycron
cat >> /tmp/mycron << EOF
*/5 * * * * env PATH=/opt/homebrew/bin:/usr/local/bin:${OPENCLAW_BIN}:/usr/bin:/bin bash -c 'date && python3 ${HOME}/src/fieldkit/platform/email-agent/scripts/check_email.py --source cron' >> ${HOME}/src/fieldkit/logs/cron.log 2>&1
EOF
crontab /tmp/mycron && rm /tmp/mycron
crontab -l | grep check_email
```

> Change `*/5` to `*/N` if you want a different polling interval.
> `date` prepends a timestamp to every entry in `cron.log` so you can see when each run fired.

> `env PATH=…` is required because cron does not source your shell profile, and
> `PATH=value cmd` only applies to that one command — subprocesses (like `openclaw`)
> would revert to cron's minimal PATH. `env` sets the PATH for `python3` and all
> processes it spawns.
> `gws` lives in `/opt/homebrew/bin` (Apple Silicon) or `/usr/local/bin` (Intel).
> `openclaw` is managed by nvm and lives in a version-specific path —
> `$(dirname $(which openclaw))` captures the correct path at registration time.
> `$HOME` and `$OPENCLAW_BIN` are expanded by your shell when you run this command,
> so the crontab stores the literal values.

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
| `check_email` shows **needs setup** in `openclaw skills list` | The metadata `requires.bins` lists `gws` and `python3` — confirm both are on PATH. Run `which gws python3` to verify. |
| `check_email` does not appear in `openclaw skills list` | The workspace copy is missing. Re-run the `cp` command in Step 8 and restart the gateway. |
| `[skills] Skipping escaped skill path … reason=symlink-escape` | You used a symlink instead of a direct copy. Remove the symlink and re-run Step 8. |
| LLM improvises instead of running the script | The skill body is not loaded. Confirm `~/.openclaw/workspace/skills/check_email/SKILL.md` exists (not a symlink), then run `openclaw gateway restart`. |
| `check_email: ADMIN_ALLOWLIST is empty` in Telegram | `.env` is missing or `ADMIN_ALLOWLIST` is blank. Check Step 7. |
| `check_email: ADMIN_TELEGRAM_CHAT_ID is not set` | `.env` is missing `ADMIN_TELEGRAM_CHAT_ID`. Check Step 7. |
| `gws gmail … failed` in Telegram | gws token may have expired. Re-run Step 6. |
| `OS keyring failed` or `Decryption failed` in Telegram | gws was authenticated with the macOS keychain, which is inaccessible from cron. Run `gws auth logout` then re-run Step 6 (with `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file`). Ensure `.env` contains `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file`. |
| `gws binary not found` in cron.log | gws is not on cron PATH. Remove the crontab entry (`crontab -e`) and re-run Step 10. |
| `openclaw: command not found` in cron.log | openclaw (nvm-managed) is not on cron PATH. Remove the entry (`crontab -e`) and re-run Step 10 — `$(dirname $(which openclaw))` captures the current nvm bin path. If you upgraded Node via nvm since registering cron, you must re-run Step 10 again. |
| `FileNotFoundError: 'openclaw'` in cron.log | Old-style `PATH=… cmd` entry — PATH assignment only applied to the first command, not to Python's subprocesses. Remove the entry (`crontab -e`) and re-run Step 10. |
| `check_email: AGENT_EMAIL is not set` in Telegram | `.env` is missing `AGENT_EMAIL`. Check Step 5. |
| Script exits with lock error | Check if another instance is still running: `pgrep -af check_email.py`. If a process is found, wait for it to finish. If no process is found but the error persists, the lock file is stale — delete it: `rm ~/src/fieldkit/data/email-agent/run.lock`. |
