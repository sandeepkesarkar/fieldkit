# Step 1 — Create the Meta Developer App

This doc follows the flow documented at
[developers.facebook.com/docs/development/create-an-app/](https://developers.facebook.com/docs/development/create-an-app/)
exactly, then covers the post-creation configuration needed for Pages posting.

**Time required**: ~15 minutes.

---

## Prerequisites

- A Facebook account. This becomes the app's Administrator.
- A Facebook Page where that account has admin access (or create one in Part E).

---

## Part A — Register as a Meta Developer (skip if already done)

1. Go to [developers.facebook.com](https://developers.facebook.com) and click **Get Started**.
2. Log in with your Facebook account.
3. Verify your phone number when prompted and accept the Meta Platform Policies.

---

## Part B — Create the App (6-step wizard)

Navigate to [developers.facebook.com/apps/creation/](https://developers.facebook.com/apps/creation/).

### Screen 1 — App details

| Field | What to enter |
|---|---|
| App name | e.g. `FieldKit Demo` |
| App contact email | your email address |

Click **Next**.

### Screen 2 — Use cases

Select **"Manage everything on your Page"**.

Its description in the UI reads:
> *"Publish content and videos, moderate posts and comments from followers on your Page and get insights on engagement."*

The other options in the list are for ads, Instagram, Messenger, Threads, fundraisers, etc. — none of them apply here.

> **This choice is what unlocks the Pages permissions.** Selecting any other use
> case will cause `pages_show_list`, `pages_read_engagement`, and
> `pages_manage_posts` to show as **"Invalid Scopes"** in the OAuth dialog.
>
> After selecting, incompatible use cases are greyed out. That is expected.

Click **Next**.

### Screen 3 — Business portfolio

Select **"I don't want to connect a business portfolio yet"**.

> A business portfolio (Meta Business Manager) is not required for a personal or
> demo setup running in Development mode.

Click **Next**.

### Screen 4 — Publishing requirements

This screen lists what you must complete before your app can go **Live** (public).
It is **informational only** — nothing here blocks you from proceeding.

**Leave everything as-is and click Next.**

Requirements shown (e.g. App Review, Privacy Policy URL, data deletion instructions)
only apply if you later want to make the app public. FieldKit stays in
Development mode — only your own account uses it — so none of these are needed.

### Screen 5 — Overview

Review the summary: app name, use case, connected business (none), and requirements.
Use the section buttons to go back and correct anything if needed.

Click **"Go to dashboard"**.

---

You are now in the App Dashboard. A status banner at the top indicates the app is
in **Development mode** — this is correct. In Development mode, only people with
a role on the app (Administrators, Developers, Testers) can authorize it.

---

## Part C — Note your App ID and App Secret

1. In the left sidebar click **App settings → Basic**.
2. At the top of the page, copy the **App ID** (numeric, visible immediately). This is your `FB_APP_ID`.
3. Next to **App Secret**, click **Show**, confirm your Facebook password, and copy the value. This is your `FB_APP_SECRET`.

> **Security rule**: `FB_APP_SECRET` is used only by `generate_auth_link.py`
> (run locally, once). It must never be committed to git, logged, or appear
> anywhere in the cron path (`upload_facebook.py`).

---

## Part D — Configure the "Manage everything on your Page" use case

After creation the dashboard shows an **"App customization and requirements"** panel
with three items. The first one is what you need:

> **Become a Tech Provider** (shown below the three items) — ignore this entirely.
> It is for apps that access other businesses' data. Not needed here.

### D1 — Open the use case customization panel

1. On the Dashboard, click **"Customize the Manage everything on your Page use case"** (the first item in the list).

You land on a **"Customize use case"** page (the left sidebar shows **Manage Pages → Customize**). It lists all available permissions, each with either a status badge or an **Add** button.

### D2 — Add pages_manage_posts and pages_read_engagement

The permissions already added by default (status shows **"Ready for testing"**):
- `business_management`
- `pages_show_list`
- `public_profile`

Scroll down the list and click **Add** next to these two:

1. **`pages_manage_posts`** — "allows your app to manage and delete posts on a Page"
2. **`pages_read_engagement`** — "allows your app to read content (posts, statuses, photos, videos) posted by the Page"

   > `pages_read_engagement` is a declared dependency of `pages_manage_posts`.
   > Both are required to post videos.

The other permissions in the list (`pages_manage_engagement`, `pages_manage_metadata`,
`pages_read_user_content`, `read_insights`, etc.) are **optional** — skip them.

Once added, both permissions will show **"Ready for testing"** status with an Actions dropdown — this confirms they are active.

After adding, your active permissions should be:
```
business_management        (default)
public_profile             (default)
pages_show_list            (default)
pages_read_engagement      ← just added
pages_manage_posts         ← just added
```

### D3 — OAuth redirect URI (no action needed for localhost)

**Facebook Login for Business** is automatically added to your app when the
"Manage Pages" use case is selected — it appears in the left sidebar as
**"Facebook Login for Bus..."**.

Facebook **automatically allows all `localhost` redirect URIs** for development
mode apps — you do not need to register `http://localhost:8080/callback`
explicitly. The "Valid OAuth Redirect URIs" field under Facebook Login for
Business → Settings is only needed for non-localhost production URIs.

> If you ever move to a non-localhost redirect URI (e.g. a hosted server), add it
> in **Facebook Login for Business → Settings → Valid OAuth Redirect URIs**, then
> click **Save changes**.

---

## Part E — Create a Facebook Page (if needed)

The account that runs the OAuth flow must be an admin of the target Page.

1. While logged in as the developer account, go to
   [facebook.com/pages/create](https://www.facebook.com/pages/create).
2. Enter a **Page name** and **Category**, then click **Create Page**.
3. Skip any optional setup prompts.

**Finding your Page ID** — you will need it for `--page-id`:

- Option 1: On the Page, click **About** in the left menu. Look for **Page ID** — it is a long number (e.g. `123456789012345`).
- Option 2: Use Graph API Explorer (covered in doc 2) — `GET /me/accounts` returns the ID alongside the token.
- Option 3: The Page URL sometimes contains the ID directly: `facebook.com/profile.php?id=123456789012345`.

---

## Part F — Add credentials to `.env`

Open `clients/<client>/src/photo-agent/.env` (copy from `.env.example` first if it doesn't exist):

```bash
FB_APP_ID=<App ID from Part C>
FB_APP_SECRET=<App Secret from Part C>
```

```bash
chmod 600 clients/<client>/src/photo-agent/.env
```

Leave `FB_PAGE_ID` and `FB_PAGE_ACCESS_TOKEN` blank for now — doc 2 covers getting those.

---

## Checkpoint

- [ ] App created with use case **"Manage everything on your Page"**
- [ ] App is in **Development mode**
- [ ] Permissions `pages_manage_posts` and `pages_read_engagement` added (status shows "Ready for testing" — this is correct for dev mode, no App Review needed)
- [ ] `FB_APP_ID` and `FB_APP_SECRET` set in `.env`
- [ ] Facebook Page exists with your account as admin, and you know the Page ID

> **Note on redirect URIs**: Facebook automatically allows all `localhost` redirect URIs in development mode. You do **not** need to add `http://localhost:8080/callback` to the valid redirect URI list. The popup warning you may see when trying to add it confirms this.

**Next**: [Get tokens and test manually →](02-manual-test.md)

---

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| "Invalid Scopes: pages_show_list…" in browser | Use case not set to "Manage everything on your Page" | Create a new app and select the correct use case — app type cannot be changed |
| Can't find "Manage everything on your Page" | Scroll down — it is about two-thirds down the use cases list | Look for the description "Publish content and videos…" |
| `pages_manage_posts` not listed in permissions | Use case was not properly applied | Re-open Dashboard → Customize and add it manually |
| "Redirect URI doesn't match" during OAuth | URI not saved in Facebook Login for Business → Settings | Re-add `http://localhost:8080/callback` and click Save |
| App Secret field shows only dots | Normal — click **Show** and confirm your Facebook password |  |
| After creation, left sidebar shows nothing | Navigate to **Dashboard** under your app name | The use case panel appears there, not under App Settings |
