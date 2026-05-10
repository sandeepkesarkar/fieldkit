# Email Agent — Mac Mini Setup

One-time setup procedure for a new Mac Mini. Run every step in order.
Estimated time: 20–25 minutes.

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

## 8 — Register the skill with OpenClaw

OpenClaw does not auto-discover skills from the filesystem. Skills must be declared in
`~/.openclaw/openclaw.json` via `skills.load.extraDirs`. Add the `skills` block shown
below — the path must point to the **parent** of the skill folder (`platform/`, not
`platform/email-agent/`), so OpenClaw resolves `email-agent/SKILL.md` the same way it
resolves its own bundled skills.

> **Do not use symlinks.** OpenClaw rejects symlinks that resolve outside the skills
> root as a security measure.

Open `~/.openclaw/openclaw.json` in any editor and add the `"skills"` key at the top
level (alongside `"tools"`, `"channels"`, etc.):

```json
"skills": {
  "load": {
    "extraDirs": [
      "/Users/<your-username>/src/fieldkit/platform"
    ]
  }
}
```

Replace `<your-username>` with the output of `whoami`. Then restart the gateway:

```bash
openclaw gateway restart
```

---

## 9 — Verify end-to-end

```bash
# gws can reach Gmail
source ~/src/fieldkit/platform/email-agent/.env
gws gmail users messages list --params '{"userId": "me", "q": "is:unread"}'
# Must return JSON (empty list is fine)

# Runtime directories exist
ls ~/src/fieldkit/data/email-agent ~/src/fieldkit/logs

# Skill is registered
openclaw skills list | grep check-email
# Must show check-email with source "openclaw-extra"
```

---

## 10 — Register the cron job (T07)

Once all verification steps above pass:

```bash
cd ~/src/fieldkit/platform/email-agent
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
| `openclaw skills list` doesn't show `check-email` | Verify Step 8 — `skills.load.extraDirs` in `~/.openclaw/openclaw.json` must point to `…/fieldkit/platform`, then run `openclaw gateway restart` |
| `[skills] Skipping escaped skill path … reason=symlink-escape` | You used a symlink — OpenClaw blocks this. Remove it and follow Step 8 instead |
