# Email Agent — Mac Mini Setup

One-time setup procedure for a new Mac Mini. Run every step in order.
Estimated time: 15–20 minutes.

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
source ~/fieldkit/platform/email-agent/.env
gws auth login --services gmail
```

A browser window opens. **Sign in with the agent Gmail account** (`$AGENT_EMAIL`), not your personal account.

---

## 6 — Create runtime directories

```bash
mkdir -p ~/fieldkit/data/email-agent ~/fieldkit/logs
```

---

## 7 — Populate `.env`

```bash
cp ~/fieldkit/platform/email-agent/.env.example \
   ~/fieldkit/platform/email-agent/.env
```

Edit `.env` and fill in:

| Variable | Value |
|----------|-------|
| `AGENT_EMAIL` | The agent Gmail address you authenticated in Step 5 |
| `ADMIN_ALLOWLIST` | Comma-separated permitted sender addresses |
| `POLLING_INTERVAL_MINUTES` | How often to poll (e.g. `5`) |

**Telegram credentials** are managed inside OpenClaw — configure them via:
```bash
openclaw config set telegram-bot-token <token>
openclaw config set telegram-chat-id <chat-id>
```

---

## 8 — Verify end-to-end

```bash
# gws can reach Gmail
source ~/fieldkit/platform/email-agent/.env
gws gmail users messages list --params '{"userId": "me", "q": "is:unread"}'
# Must return JSON (empty list is fine)

# Runtime directories exist
ls ~/fieldkit/data/email-agent ~/fieldkit/logs

# Skill loads
openclaw skills list
# Must show check-email with no errors
```

---

## 9 — Register the cron job (T07)

Once all verification steps above pass:

```bash
cd ~/fieldkit/platform/email-agent
source .env && openclaw cron add \
  --name "email-agent-poll" \
  --cron "*/${POLLING_INTERVAL_MINUTES} * * * *" \
  --session isolated \
  --message "Check Gmail inbox for new emails and process them per the email agent skill"

openclaw cron list
# Must show email-agent-poll
```

---

## Troubleshooting

| Error | Fix |
|-------|-----|
| `gws: command not found` | Run Step 3 |
| `gcloud CLI not found` | Run Step 2, open a new terminal after install |
| `No OAuth client configured` | Run Step 4 |
| `gws auth login` fails | Re-run Step 5 — ensure you sign in with `$AGENT_EMAIL` |
| `openclaw skills list` doesn't show `check-email` | Verify `SKILL.md` is in `~/fieldkit/platform/email-agent/` |
