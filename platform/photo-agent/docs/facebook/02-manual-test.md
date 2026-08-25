# Step 2 — Get Tokens and Test Manually

Before running `generate_auth_link.py`, verify that your app is wired up correctly
by getting a Page access token manually and making a real API call. This confirms
every permission is in place before you automate anything.

**Time required**: ~10 minutes.

**Prerequisite**: [doc 1 (Create the app)](01-create-app.md) complete.

---

## Part A — Get a short-lived User token via Graph API Explorer

The Graph API Explorer is Meta's browser-based tool for making API calls with
a real token, without writing any code.

1. Go to [developers.facebook.com/tools/explorer](https://developers.facebook.com/tools/explorer).
   - If you are redirected to a login page, sign in with the same Facebook account you used to create the app.

2. In the top-right dropdown labelled **Meta App**, select your app (e.g. **FieldKit Demo**).

3. Next to it, find the **"User or Page"** token-type dropdown and explicitly set it
   to **User Token**. It defaults to whatever was last selected in your browser —
   which may be **Page Token** left over from a previous session — and the Explorer
   does not warn you if it's wrong. Generating a Page token here by mistake produces
   a confusing failure later (see [Troubleshooting](#troubleshooting) below).

4. Click **Generate Access Token**.

5. A permissions dialog appears. Before clicking anything, expand the **permissions** panel on the right side of the Explorer and add:
   - `pages_show_list`
   - `pages_read_engagement`
   - `pages_manage_posts`

   Then click **Generate Access Token** again (or the dialog may already be open — check it includes those scopes).

6. Facebook shows a login/permissions screen. Click **Continue as [your name]**, then **Save**.

   > If you see "Invalid Scopes" or any permission is greyed out, your app's use case
   > is not configured correctly. Return to [doc 1, Step 5](01-create-app.md) and
   > verify the permissions are added.

7. The Explorer shows a token in the **Access Token** field. This is a **short-lived user access token** (valid ~1 hour). Copy it.

---

## Part B — Verify the token and list your Pages

Still in the Graph API Explorer, in the query field at the top:

1. Change the method to **GET** and set the path to:
   ```
   /me/accounts
   ```
2. Click **Submit**.

The response should look like:
```json
{
  "data": [
    {
      "access_token": "EAAabc...XYZ",
      "category": "Local business",
      "category_list": [...],
      "name": "My Test Page",
      "id": "123456789012345",
      "tasks": ["ANALYZE", "CREATE_CONTENT", "MODERATE", "ADVERTISE", "MANAGE"]
    }
  ]
}
```

> **If your account admins more than one Page**, `data` will have multiple entries.
> Match the entry by its **`name`** field — not by any Page ID you already have
> from the browser or the Page's URL, which can be a completely different
> identifier (see [doc 1, Part E](01-create-app.md)). Use *that entry's* `id`
> field as your `FB_PAGE_ID`.

- **`id`** is your `FB_PAGE_ID`. Copy it.
- **`access_token`** in the response is a **Page access token**. Copy it.
- The `tasks` list must contain **`CREATE_CONTENT`** — this is what allows posting.

> If the `data` array is empty, the Facebook account you authorised with is not
> an admin of any Page. Make sure you are logged in as the Page admin and re-run
> the Explorer flow from Part A.
>
> If you instead get an error like `"Tried accessing nonexisting field (accounts)"`,
> the token in the Explorer's Access Token field is a **Page** token, not a **User**
> token — Page nodes have no `/accounts` edge. Go back to Part A and confirm the
> token-type dropdown was set to **User Token**. See
> [Troubleshooting](#troubleshooting) for how to positively identify a token's type.

---

## Part C — Test a Page API call

Using the Page access token from Part B, make a test post to your Page.

### Option 1: Graph API Explorer (easiest)

1. In the Explorer, paste the **Page access token** from Part B into the **Access Token** field (replacing the user token).
2. Change the method to **POST** and the path to:
   ```
   /{your-page-id}/feed
   ```
   Replace `{your-page-id}` with the numeric ID from Part B.
3. In the request body, add a JSON field:
   ```json
   { "message": "FieldKit test post — safe to delete" }
   ```
4. Click **Submit**.

Success response:
```json
{ "id": "123456789012345_987654321098765" }
```

Check your Facebook Page — the test post should appear. Delete it manually after.

### Option 2: curl

```bash
curl -s -X POST \
  "https://graph.facebook.com/v25.0/{your-page-id}/feed" \
  -H "Content-Type: application/json" \
  -d '{"message":"FieldKit test post — safe to delete","access_token":"PASTE_PAGE_TOKEN_HERE"}'
```

Expected output:
```json
{"id":"123456789012345_987654321098765"}
```

---

## Part D — Exchange for a long-lived Page token (required)

The Page access token from Part B is short-lived if it was derived from a short-lived user token. `generate_auth_link.py` handles this automatically, but here's how to do it manually to understand the chain.

### Step 1: Exchange short-lived user token → long-lived user token

```bash
curl -s "https://graph.facebook.com/v25.0/oauth/access_token
  ?grant_type=fb_exchange_token
  &client_id=YOUR_FB_APP_ID
  &client_secret=YOUR_FB_APP_SECRET
  &fb_exchange_token=SHORT_LIVED_USER_TOKEN"
```

Response:
```json
{
  "access_token": "EAAabc...LONG_TOKEN",
  "token_type": "bearer",
  "expires_in": 5183944
}
```

The `expires_in` is ~60 days. Save this long-lived user token.

### Step 2: Exchange long-lived user token → Page access token

```bash
curl -s "https://graph.facebook.com/v25.0/me/accounts
  ?access_token=LONG_LIVED_USER_TOKEN" | python3 -m json.tool
```

Piping through `python3 -m json.tool` pretty-prints the response — worth doing any
time your account admins more than one Page, so you can visually match each
`name` to its `id` instead of picking through one unbroken line of JSON. As in
Part B, if more than one Page comes back, match by **`name`** — don't assume the
first entry, and don't substitute a Page ID you already have from the browser.

> Current Meta docs favor the explicit form `GET /{user-id}/accounts` over the
> `/me/accounts` alias used above. They are functionally equivalent today, but
> the explicit form is the more future-proof one to reach for. Get your user ID
> first with `GET /me?access_token=LONG_LIVED_USER_TOKEN`, then substitute it in.

The `access_token` values in the response's `data` array are now **long-lived Page access tokens that do not expire** (they are only invalidated if the user changes their password, deauthorises the app, or the Page admin role is removed).

Write the values to `.env`:
```bash
FB_PAGE_ID=123456789012345
FB_PAGE_ACCESS_TOKEN=EAAabc...PERMANENT_PAGE_TOKEN
```

---

## Part E — Verify the token details

Use the Token Debugger to confirm your Page token is valid and has the right scopes:

1. Go to [developers.facebook.com/tools/debug/accesstoken/](https://developers.facebook.com/tools/debug/accesstoken/).
2. Paste your Page access token into the **Access Token** field and click **Debug**.

Look for:
- **App** — must match your app name
- **Type** — should be `Page`
- **Expires** — should say "Never" for a long-lived Page token
- **Scopes** — must include `pages_manage_posts`, `pages_read_engagement`, `pages_show_list`

If any scope is missing, go back to Part A, include the missing permission explicitly, and redo the token chain.

---

## Checkpoint

At the end of this step you should have verified:

- [ ] `GET /me/accounts` returns your Page with `CREATE_CONTENT` in its tasks
- [ ] `POST /{page_id}/feed` with a Page token creates a real post (and you deleted it)
- [ ] Token Debugger shows the Page token is valid, type `Page`, expires `Never`
- [ ] `FB_PAGE_ID` and `FB_PAGE_ACCESS_TOKEN` are set in `.env`

If all four boxes are checked, your app is fully operational. You can stop here for
manual use, or proceed to `generate_auth_link.py` to automate the token flow:

```bash
python3 scripts/generate_auth_link.py --page-id YOUR_PAGE_ID
```

---

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| `GET /me/accounts` returns empty `data` | Account is not a Page admin | Log in as the admin account, or add this account as admin on the Page |
| `tasks` list missing `CREATE_CONTENT` | Account has a restricted Page role | Upgrade to full admin on the Page via Page Settings → Page Roles |
| Token debugger shows wrong scopes | Permissions not added to the use case | Doc 1 Step D2, re-add permissions, re-generate token |
| `POST /feed` returns "Permissions error" | Using user token instead of Page token | Use the `access_token` from `/me/accounts` response, not the Explorer token |
| Token expires in 1 hour | Short-lived user token used to call `/accounts` | Do Part D to exchange for a permanent Page token |
| "This app is in development mode" error | The account has no role on the app | Add the account as Tester under App Roles → Roles in the app dashboard |
| "App not active" when opening the OAuth URL | The Meta app may need a Business Manager connection, or the app is in a state that blocks the OAuth dialog | Use the manual token flow in this doc (Parts A–D) instead of `generate_auth_link.py`. Both produce the same long-lived Page token. |
| `(#100) Tried accessing nonexisting field (accounts)` calling `/me/accounts` or `fb_exchange_token` | You passed a **Page** access token where a **User** token was expected — Page nodes have no `/accounts` edge | Confirm the token type before retrying blind: `curl -s "https://graph.facebook.com/v25.0/debug_token?input_token=TOKEN&access_token=APP_ID\|APP_SECRET"`. `"type":"PAGE"` (with a `profile_id`) means it's a Page token; `"type":"USER"` (with a `user_id`, no `profile_id`) means it's a User token. Go back to Part A and regenerate with the token-type dropdown set to **User Token**. |
| `FacebookUploadError: global id X is not allowed` | Wrong Page ID in `.env` | Confirm the Page ID from the `id` field in `GET /me/accounts` response — not the URL or profile ID. For Pages using Meta's newer unified Page UI, the `profile.php?id=...` number in the URL is a **different ID** from the real Graph API Page ID — match by the Page's `name` in the `/me/accounts` response instead (see [doc 1, Part E](01-create-app.md)). |
| `FacebookUploadError: Application has been deleted` (code 101) | Wrong `FB_APP_ID` or `FB_APP_SECRET` in `.env` | Copy the exact values from App Settings → Basic in the developer console |
