# SKILL: generate_auth_link.py — Automated Facebook Page Token Setup

Admin-only CLI that runs the full Facebook OAuth flow and writes a permanent
Page access token to `.env` automatically.

> **First time?** Work through the detailed setup docs first:
> 1. [docs/facebook/01-create-app.md](docs/facebook/01-create-app.md) — create the Meta developer app
> 2. [docs/facebook/02-manual-test.md](docs/facebook/02-manual-test.md) — verify it works manually and get your first long-lived Page token
>
> Then come back here to automate future token refreshes.

> **Known issue — "App not active" during OAuth**: Some Meta app configurations show an
> "App not active" error when opening the OAuth URL in a browser, even when logged in as
> the app admin. If this happens, use the manual token flow in
> [docs/facebook/02-manual-test.md](docs/facebook/02-manual-test.md) (Parts A–D) to get a
> long-lived Page token and write it to `.env` directly. The result is identical.

---

## Prerequisites

- Meta developer app created with the **"Manage everything on your Page"** use case
- `http://localhost:8080/callback` added as a valid redirect URI in Facebook Login for Business
- `FB_APP_ID` and `FB_APP_SECRET` set in `.env`
- A Facebook Page where your account has admin access

---

## Usage

```bash
cd platform/photo-agent
python3 scripts/generate_auth_link.py --page-id YOUR_PAGE_ID
```

| Argument | Default | Description |
|---|---|---|
| `--page-id` | — | Facebook Page ID (numeric). Required. |
| `--port` | 8080 | Port for the local OAuth callback server. Update the redirect URI in your app if you change this. |

---

## What it does

1. Reads `FB_APP_ID`, `FB_APP_SECRET` from `.env` — exits 1 if missing.
2. Builds a Facebook OAuth URL with scopes `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`.
3. Prints the URL and starts a local server on `localhost:PORT`.
4. Waits for you to complete the OAuth flow in a browser.
5. Exchanges the authorization code for a short-lived user token, then a long-lived user token (~60 days), then a permanent Page access token (never expires).
6. Writes `FB_PAGE_ID` and `FB_PAGE_ACCESS_TOKEN` to `.env` (preserves all other vars).

---

## Example session

```
Facebook authorization URL:
https://www.facebook.com/dialog/oauth?client_id=...&scope=pages_show_list%2Cpages_read_engagement%2Cpages_manage_posts&...

Waiting for authorization on http://localhost:8080/callback ...
Authorization complete. Page access token written to .env.
Linked Page ID: 123456789012345
```

Open the URL in a browser while logged in as the Page admin, click **Save**, and the script catches the redirect automatically.

---

## Verify

```bash
grep FB_PAGE .env
# FB_PAGE_ID=123456789012345
# FB_PAGE_ACCESS_TOKEN=EAAabc... (100+ chars, never expires)
```

Use the [Token Debugger](https://developers.facebook.com/tools/debug/accesstoken/) to confirm the token type is `Page` and Expires shows `Never`.

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success — token written to .env |
| 1 | Missing `FB_APP_ID` or `FB_APP_SECRET` |
| 2 | OAuth flow failed (user denied, bad code, network error) |
| 3 | Page selection failed (page not found, or account not admin) |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "Invalid Scopes" in browser | App use case is wrong — see [01-create-app.md](docs/facebook/01-create-app.md) Step 2 |
| Port already in use | Run with `--port 8081`; add `http://localhost:8081/callback` to redirect URIs |
| "Page not found" (exit 3) | Verify the Page ID and that the authorizing account is a Page admin |
| Token expired later | Re-run this script — it always produces a non-expiring Page token |
