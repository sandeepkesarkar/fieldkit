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

## 5 — Authenticate against the agent Gmail account

```bash
source ~/src/fieldkit/platform/email-agent/.env
gws auth login --services gmail
```

A browser window opens. **Sign in with the agent Gmail account** (`$AGENT_EMAIL`), not your personal account.

---

## 6 — Create runtime directories

```bash
mkdir -p ~/src/fieldkit/data/email-agent ~/src/fieldkit/logs
```

---

## 7 — Populate `.env`

```bash
cp ~/src/fieldkit/platform/email-agent/.env.example \
   ~/src/fieldkit/platform/email-agent/.env
```

Edit `.env` and fill in all four variables:

| Variable | Value |
|----------|-------|
| `AGENT_EMAIL` | The agent Gmail address you authenticated in Step 5 |
| `ADMIN_ALLOWLIST` | Comma-separated permitted sender addresses |
| `POLLING_INTERVAL_MINUTES` | How often to poll (e.g. `5`) |
| `ADMIN_TELEGRAM_CHAT_ID` | Your Telegram chat ID (see note below) |

**Finding `ADMIN_TELEGRAM_CHAT_ID`:** send any message to your OpenClaw bot in Telegram, then run:

```bash
grep "sendMessage ok" ~/.openclaw/logs/gateway.log | tail -5
```

The chat ID appears after `chat=`.

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

```bash
(crontab -l 2>/dev/null; echo "*/5 * * * * cd $HOME/src/fieldkit/platform/email-agent && python3 scripts/check_email.py --source cron >> $HOME/src/fieldkit/logs/cron.log 2>&1") | crontab -
```

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
| `gws gmail … failed` in Telegram | gws token may have expired. Re-run Step 5. |
| Script exits with lock error | Another instance is running. Wait 30 seconds and retry. |
