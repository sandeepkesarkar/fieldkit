# Instagram Integration — Setup Docs

Feature 005 publishes an approved video to a client's Instagram professional
account as a Reel, from the **same** Telegram approval that already triggers the
Facebook post. There is no second approval step and no second bot.

Unlike the Facebook integration, this one needs **no new Meta Developer App and
no new OAuth flow** — which is why there is no `01-create-app.md` here. Everything
below rides on the Facebook Page connection you already made in
[`../facebook/`](../facebook/README.md).

It does **not** ride on that connection's existing *permissions*, though. The Page
token issued for Facebook publishing lacks the two Instagram scopes, so the same
authorization flow must be run once more to add them — see Step 0 below.

---

## Why there is no new app or token — but there are new scopes

Instagram content publishing for professional accounts is served by the
**Facebook** Graph API (`graph.facebook.com`), through the Facebook Page the
Instagram account is linked to. An Instagram account must already be a
Business/Creator account linked to a Page before the API can publish to it at
all. When that's true, publishing uses the same Meta app and the same *kind* of
Page access token FieldKit already has from Feature 003.

It does **not** use that token *as issued*. An earlier version of this page said
the existing Page token was sufficient and no new scopes were needed. That was
wrong. A live check of the `_demo` Page token found exactly these scopes:

- `pages_show_list`
- `pages_read_engagement`
- `pages_manage_posts`

Instagram publishing additionally needs:

- `instagram_basic` — read the linked Instagram account
- `instagram_content_publish` — create and publish media containers

A token without them cannot publish to Instagram, however it was obtained, so it
has to be re-issued with them. See Step 0.

So this feature adds exactly one new environment variable:

| Variable | What it is | Where it comes from |
|---|---|---|
| `IG_BUSINESS_ACCOUNT_ID` | Instagram professional account ID (numeric) | Written by `check_instagram_connection.py` |

`IG_BUSINESS_ACCOUNT_ID` is a **public account identifier, not a secret**. No new
app secret or credential class is introduced anywhere in Feature 005.
`FB_PAGE_ACCESS_TOKEN` is re-issued with two more scopes (Step 0) but stays the
same variable, holding the same kind of token.

**If `IG_BUSINESS_ACCOUNT_ID` is absent or empty, Instagram publishing is off for
that client** — `check_approval.py` enqueues no Instagram job and
`upload_instagram.py` exits 0 without touching state. That absence is the entire
per-client enable switch; there is no client-name special-casing in the code.

---

## Prerequisites

1. The client's Facebook Page is already connected (see
   [`../facebook/README.md`](../facebook/README.md)) — `FB_PAGE_ID` and
   `FB_PAGE_ACCESS_TOKEN` are in the client's `.env`.
2. The client's Instagram account is a **Business or Creator** account.
   In the Instagram app: *Settings → Account type and tools → Switch to
   professional account*.
3. That Instagram account is **linked to the Facebook Page**.
   In Meta Business Suite / Page settings: *Linked accounts → Instagram → Connect*.
   Reference: <https://www.facebook.com/business/help/898752960195806>

4. The Meta app has **`instagram_basic`** and **`instagram_content_publish`**
   enabled, the same way `pages_manage_posts` was enabled for Facebook (see
   [`../facebook/01-create-app.md`](../facebook/01-create-app.md), section D2). If they
   are not enabled, Step 0's OAuth dialog reports "Invalid Scopes".

Steps 2, 3 and 4 are done by a human in Meta's UI. They are not automatable, and
`check_instagram_connection.py` will tell you clearly if 2 or 3 is missing.

---

## Step 0 — Re-authorize with the Instagram scopes (one-time, per client)

Re-run the Facebook authorization flow, this time with `--instagram`:

```bash
cd platform/photo-agent
CLIENT_NAME=_demo python3 scripts/generate_auth_link.py --page-id 123456789 --instagram
```

This requests the three Pages scopes **plus** `instagram_basic` and
`instagram_content_publish`, and writes the resulting Page token over the existing
`FB_PAGE_ACCESS_TOKEN`. Facebook publishing keeps working with the new token, since
the Pages scopes are all still there.

Why `--instagram` is a flag rather than always on: Meta rejects any requested scope
the app has not enabled. If the Instagram scopes were always requested, this script
— the only way to reconnect Facebook — would fail on an app not set up for Instagram
(prerequisite 4).

---

## Step 1 — Link the account (one-time, per client)

Run the connection check from `platform/photo-agent/`:

```bash
cd platform/photo-agent
CLIENT_NAME=_demo python3 scripts/check_instagram_connection.py
```

Optionally target a specific Page instead of the `.env` value:

```bash
CLIENT_NAME=_demo python3 scripts/check_instagram_connection.py --page-id 123456789
```

**On success** it writes `IG_BUSINESS_ACCOUNT_ID` into
`clients/<client>/src/photo-agent/.env` (preserving every other variable) and prints:

```
Checking Facebook Page 123456789 for a linked Instagram account...
Found linked Instagram account: @my_business_demo (ID: 17841400000000000)
Account type: BUSINESS
Instagram publishing enabled. IG_BUSINESS_ACCOUNT_ID written to .env.
```

**Exit codes:**

| Code | Meaning | What to do |
|---|---|---|
| `0` | Success | Nothing — Instagram publishing is now enabled for this client |
| `1` | `FB_PAGE_ACCESS_TOKEN` / `FB_PAGE_ID` missing, token expired, or the API call failed | Re-run `generate_auth_link.py`, or retry if it was transient |
| `3` | No linked Instagram account, or the linked one is `PERSONAL` | Do prerequisites 2/3 above, then re-run |

Both exit-3 cases print specific, actionable guidance — not a stack trace. The
`PERSONAL` case tells you to convert the account; the not-linked case tells you
to link one.

Re-running the script against an already-configured client is safe: it updates
`IG_BUSINESS_ACCOUNT_ID` in place rather than appending a duplicate.

---

## Step 2 — Install the cron entry

> **This step is not optional, and FieldKit will not let you skip it.** Until this
> cron entry is running, approving a video queues **nothing** for Instagram, and
> the owner is told why. See "If the cron is missing" below.

`upload_instagram.py` is cron-invoked, on the same cadence as
`upload_facebook.py`. Add it to `crontab -e` alongside the existing entries:

```cron
* * * * * /usr/local/bin/python3 /path/to/fieldkit/platform/photo-agent/scripts/upload_instagram.py --source cron >> /path/to/fieldkit/logs/cron.log 2>&1
```

Verify it is really running before relying on it. One minute after saving the
crontab, the heartbeat file should exist and carry a recent `instagram` timestamp.

`FIELDKIT_DATA_DIR` is set in the **client** `.env`, not in your shell, so read it
from there rather than expecting it to be exported — run this from the fieldkit
checkout:

```bash
client=$(sed -n 's/^CLIENT_NAME=//p' .env)
data_dir=$(sed -n 's/^FIELDKIT_DATA_DIR=//p' "clients/$client/src/photo-agent/.env")
cat "$data_dir/photo-agent/worker_health.json"
```

With the conventional layout from `.env.example` that resolves to
`clients/<client>/data/photo-agent/worker_health.json`, which you can also just
`cat` directly. Either way you want something like this, with a timestamp from the
last minute or two:

```json
{
  "instagram": { "last_seen_at": "2026-09-22T09:41:00.123456+00:00" },
  "facebook":  { "last_seen_at": "2026-09-22T09:41:00.098765+00:00" }
}
```

No file, no `instagram` key, or a timestamp more than an hour old all mean the same
thing: the cron is not running, and approvals will not queue Instagram jobs.

The two upload scripts are independent: separate state files, separate lock
files, separate claim namespaces. Neither serializes against the other, and
neither can block, retry, or roll back the other's post.

A tick with nothing to do — no pending job, a claim declined, or Instagram not
configured for this client — exits `0` silently and costs nothing.

### If the cron is missing

Setting `IG_BUSINESS_ACCOUNT_ID` and installing this crontab entry are two
separate acts, and nothing in a git repository can make them atomic — a repo
cannot install a crontab entry, and an entry can be removed again afterwards. So
FieldKit detects the gap at runtime instead of assuming it away.

Every tick of either upload cron stamps a heartbeat in
`$FIELDKIT_DATA_DIR/photo-agent/worker_health.json` (see
`tools/worker_health.py`). A heartbeat is proof the cron entry exists and fires —
a different fact from the platform being switched on in `.env`. Two things read it:

- **`check_approval.py` refuses to enqueue an Instagram job** when
  `IG_BUSINESS_ACCOUNT_ID` is set but no fresh Instagram heartbeat exists. It logs
  `IG_NOWORKER` and alerts the admin naming the missing step. The Facebook enqueue
  and the approval itself are unaffected (FR-013).
- **`tools/upload_cleanup.py` stops waiting on a platform whose worker has gone
  quiet**, so a cron removed *after* a job was queued cannot strand the shared
  local video indefinitely either.

Why refuse rather than queue and hope: a job nothing drains never publishes, and
it used to make `upload_facebook.py` retain the shared local video forever waiting
on it. Refusing means no job, no retained video, and — the point that matters most
— **no temporary public Drive link is ever created without a running worker able to
revoke it.**

Both directions self-heal with no other action: install the cron and the next
approval goes through; remove it and the gap is noticed within the hour.

---

## Required environment variables

All in `clients/<client>/src/photo-agent/.env`:

| Variable | Purpose | Feature |
|---|---|---|
| `IG_BUSINESS_ACCOUNT_ID` | Target Instagram account; absent = Instagram off | 005 (new) |
| `FB_PAGE_ACCESS_TOKEN` | Reused for every Instagram Graph API call | 003 |
| `FB_PAGE_ID` | Read by `check_instagram_connection.py` | 003 |
| `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_CHAT_ID` | Success/failure notifications | 001/002 |
| `DRIVE_ROOT_FOLDER_ID` | Where the temporary share link's file is uploaded | 002 |
| `FIELDKIT_DATA_DIR` | Holds `instagram_state.json`, `upload_instagram.lock`, and `worker_health.json` | platform |
| `FIELDKIT_LOG_DIR` | Holds `photo-agent.log` | platform |

---

## How a publish actually works (and why Drive is involved)

Instagram's content-publishing endpoint does **not** accept uploaded bytes. It
takes a `video_url` that Instagram's own servers fetch, and it ingests video
asynchronously. So each attempt is:

```
create temporary Drive share link   (drive.create_temporary_share_link)
        ↓
POST /{ig_user_id}/media            (create container, media_type=REELS)
        ↓
GET  /{container_id}?fields=status_code   ← poll every 5s, cap 300s
        ↓  FINISHED
POST /{ig_user_id}/media_publish    (publish)  → returns a MEDIA ID
        ↓
GET  /{media_id}?fields=permalink   (fetch the real post URL)
        ↓
revoke the Drive share link         (drive.revoke_share_link)
```

**On the permalink.** `media_publish` returns a Graph API *media ID*, which is not a
URL and cannot be turned into one by string formatting — Instagram's public links use
an unrelated shortcode (`instagram.com/reel/<shortcode>/`). The permalink is therefore
read back with a separate call and is what appears in the Telegram confirmation. If
that lookup fails, the Reel is still live and still recorded as published; the
confirmation just says the link could not be fetched rather than offering a URL that
would not resolve.

**About the share link.** The Mac Mini has no public web server, so the approved
video is briefly published through Drive — the framework's already-sanctioned
host for client-approved media. The exposure is deliberately bounded:

- it covers exactly one already-approved video
- it is the **same metadata-stripped asset the Facebook upload posts** — the video
  is never re-processed or re-encoded for Instagram
- the link is created immediately before the container call and revoked on
  **every** exit path: success, transient failure, and token expiry
- the permission is `{"role": "reader", "type": "anyone"}` with
  `allowFileDiscovery` unset, so it is link-access, not search-discoverable
- the cleanup obligation is recorded **before the file is made public**, not after
  the share call returns

**Why the obligation is recorded first.** `drive.create_temporary_share_link()`
hands the caller the new file's ID through an `on_file_id` hook the moment the
file is uploaded and *before* any permission is granted. Recording it afterwards
left one unrecoverable case: if the permission call succeeds on Google's side and
its response is then lost to a timeout or a crash, the link is real, the call
raises, and a caller that only learns the ID from the returned URL holds nothing to
revoke — an untracked public link, forever. Registering first means the ID is
written down regardless of how that call turns out, and revoking a file that never
actually became public is a harmless no-op.

This is also *the* time bound on the exposure, because Drive cannot provide one.
The Drive API's `permissions.expirationTime` is restricted to user and group
permissions — an `anyone` permission cannot carry one — so there is no server-side
way to make an anonymous link self-destruct. Enforcement is FieldKit's, and it is
enforced before the exposure exists: worst case, one attempt plus one cron tick,
even if the process is killed at the worst possible moment.

**If a revoke fails**, it is never written off as success. The Drive file ID is
recorded durably in `instagram_state.json` under `pending_share_cleanups`, the
admin gets a Telegram alert naming that specific file, and **every subsequent cron
tick retries the revocation** — including ticks with no new video to publish —
until it succeeds, at which point the entry is cleared. A public link can never be
left dangling with nothing recording it.

Two properties of that retry loop are load-bearing:

- **It is not gated on Instagram being configured.** The cleanup pass runs before
  the `IG_BUSINESS_ACCOUNT_ID` and `FB_PAGE_ACCESS_TOKEN` checks, so clearing a
  client's Instagram config or letting its Meta token expire does *not* strand a
  link that is already public. Revoking a Drive permission needs Drive credentials
  and nothing else. (The only thing that gates it is `FIELDKIT_DATA_DIR` /
  `FIELDKIT_LOG_DIR`, which the state file itself lives under.)
- **The admin is reminded daily, not once.** The first failure alerts immediately;
  after that, a still-unrevoked link re-alerts every 24 hours
  (`_SHARE_CLEANUP_ALERT_INTERVAL_SECONDS` in `instagram_state.py`), reporting the
  running failure count. A single alert would let a permanently-failing cleanup go
  quiet while the video stayed public.

To audit: `pending_share_cleanups` in `instagram_state.json` is the authoritative
list of links that may still be public. An empty list means nothing is outstanding.
Each entry carries `attempts`, `recorded_at`, `last_attempt_at`, and
`last_alerted_at`.

### Who deletes the local video

**Neither script owns this.** One approval produces one file on disk with two
independently-scheduled consumers, so **whichever enabled platform resolves last
deletes it** — see `tools/upload_cleanup.py`. "Resolves" means reaches a terminal
state: published *or* terminally failed. A platform that isn't enabled for the
client is never waited on.

This matters: `upload_facebook.py` used to delete the file on its own successful
publish, which was correct while it was the only consumer. Left unchanged, it would
delete the video out from under a still-pending Instagram job, which would then find
the file missing and terminally fail — publishing nothing and alerting nobody.

Each script records its own terminal state *before* checking the other's, which is
what makes the check free of the **concurrency** race: if both resolve at nearly the
same moment, each one's resolution is already durable before it reads the other's, so
at least one must observe the other as terminal and delete.

That ordering does **not** give crash safety, and no ordering of two independent
processes can. If a script records its terminal state and is killed before it
consults the other, both records are clear, no later job will run cleanup for that
key, and the file is leaked. So there is a recovery pass rather than a claim that it
cannot happen: `upload_cleanup.sweep_orphaned_videos()` runs on **every tick of both
crons** and deletes any video in `VIDEO_TMP_DIR` that

- is older than 48 hours (comfortably longer than an overnight approval), **and**
- is not referenced by a pending approval, **and**
- is not referenced by an outstanding upload job on any platform.

Running it from both crons is what keeps the sweep alive when only one of the two
platforms is deployed. A video awaiting a human's approval has no upload job at all,
which is why the pending-approval check is there and not optional.

---

## Retries and failure handling

Handled by `instagram_state.claim_pending_upload()`, not by hand-rolled timing
in the script:

- **3 attempts**, with a **60-second cooldown** between them
- A transient failure releases the claim; the next cron tick retries
- After the 3rd failure: the job is marked failed, `IG_EXHAUSTED` is logged, and
  the owner gets `⚠️ Instagram upload failed for <project> after 3 attempts`
- **Token expiry is terminal after one attempt** — retrying can't fix it, so the
  owner is alerted immediately to reconnect the Page. If the token died at or after
  the publish step, the alert additionally says the Reel may be live: reconciliation
  is impossible there (asking Instagram is the very call that just failed), so the
  owner is the only remaining check
- A container stuck in processing past 300s is treated as an ordinary transient
  failure and retried

### Duplicate publication (FR-011)

`publish_container()` is the irreversible external side effect; `mark_published()`
is the durable record of it. A crash, a kill, or a lost HTTP response *between the
two* leaves Meta holding a live Reel that FieldKit has no record of — and the
re-entrancy lock cannot help, because the process holding it is already dead.

So the container ID is persisted and **survives across attempts**, and no attempt
publishes anything while a previous container's fate is unknown. Before acting, the
script asks Instagram what became of it:

| `status_code` | Action |
|---|---|
| `PUBLISHED` | Already live. Recorded via `record_recovered_publish()`, logged `IG_RECOVER`, owner told the Reel is up. **Never republished.** |
| `FINISHED` | Ingested, not published. That same container is published — the duplicate-free way to finish. |
| `ERROR`, `EXPIRED` | Definitively never published and unusable. A fresh container is safe. |
| anything else, or unreachable | Treated as a retryable failure. Not knowing a container's fate is never grounds for creating a second one. |

The same check runs when the attempt budget is exhausted, because the final attempt
can die after publishing exactly like any other — and by then the pending record is
already cleared, so this is the last chance to notice.

A recovered publish records `ig_post_id: null` and the container ID instead. The
media ID is genuinely unknowable after the fact: the Graph API offers no
container → media lookup, and guessing from the account's recent media could just as
easily match something a human posted. The Telegram message says so rather than
inventing a link.

**When the question cannot be answered at all.** Surviving *attempts* is not enough
on its own: if the publish lands, its response is lost, and the container then cannot
be reconciled for the whole retry budget, `mark_failed()` would discard the record —
and the container ID with it — leaving nothing that could ever check again and no
reason to refuse a later re-approval. A Telegram warning is not a control here; it
asks a person to remember a caveat at the exact moment the system has told them the
upload failed.

So an unsettled container is **quarantined durably** in
`pending_publish_reconciliations`, and that entry:

- **outlives the job.** It is stored outside the upload record, so `mark_failed()`
  clearing the record does not touch it.
- **blocks its idempotency key.** `check_approval.py` refuses to re-queue that video
  and says why (`IG_BLOCKED`); `set_pending_upload()` refuses it too, as a backstop.
  Note this is what an idempotency check alone cannot do — a publish whose response
  was lost never reached `published_idempotency_keys`.
- **keeps being retried.** `_drain_publish_reconciliations()` asks Instagram again on
  every tick, including ticks with no job and ticks where Instagram is no longer
  enabled for the client.
- **clears only on a definitive answer.** `PUBLISHED` → recorded and the key retired
  permanently; `FINISHED` / `ERROR` / `EXPIRED` → never published, quarantine lifted
  (`IG_RESOLVED`), owner told it is safe to re-approve.

The entry is created **in the same locked state transaction** as the removal that
makes it necessary — not afterwards by the caller. Leaving it to the caller meant a
process that died between the clear landing on disk and the caller quarantining left
neither a job nor an obligation, and the next re-approval could publish a duplicate.

The invariant, enforced in `tools/instagram_state.py`: **a pending record carries an
unresolved-publish marker (`publish_attempted_at`, alongside its `container_id`) if and
only if FieldKit asked Meta to publish that container and has not since established what
happened.** `mark_publish_attempted()` sets it before the irreversible call;
`mark_publish_settled()` clears it when Instagram reports the container as never
published; `mark_published()` and `record_recovered_publish()` retire any quarantine for
the key in the same transaction that records the publish, so a resolved obligation cannot
outlive its own resolution.

It is enforced at **one chokepoint**, not at each mutation site. Four separate sites lost
this obligation across successive reviews — the exhausted claim, `stale_failed`,
`mark_failed()`, and `set_pending_upload()` overwriting a live record — every one found
by enumerating sites, and enumeration kept missing one. So every write now passes through
`_transaction()`, which compares the pending record the transaction started with against
the one it leaves behind and carries any unresolved obligation across. Removal and
replacement are the same event as far as the obligation is concerned. A mutation site
cannot opt out, and `tests/test_instagram_state.py` asserts against the module source
that `_write()` is reachable from nowhere else, plus checks the invariant across every
mutation entry point it derives from the module rather than from a hand-written list.

### What "atomic" does and does not mean here

Precisely, because the distinction matters: the transaction is atomic **with respect to
other processes**. The exclusive `flock` is held across the whole read-modify-write, and
every mutation lands in a single `_write()` call, so no other process can observe or
interleave a half-applied state.

It is **not crash-atomic**. `_write()` overwrites and truncates the live JSON in place and
then `fsync`s; a crash mid-write can leave torn or truncated JSON, and an `fsync` failure
leaves durability indeterminate. So "the entry and the removal reach disk together" is not
a guarantee this implementation can make, and is not claimed.

What makes that survivable is that **`_read()` fails closed** — but that is worth stating
one shape at a time rather than as a general property, because stating it generally is how
it was got wrong once already:

| Torn shape | Behaviour |
|---|---|
| Malformed / truncated JSON | **fails closed** |
| Present but zero length | **fails closed** |
| Parses, but not a JSON object | **fails closed** |
| Parses as `{}`, or any object with no recognised top-level key | **fails closed** |
| Absent file | reads as fresh state — *intended*, that is a new client |
| Parses, carries a recognised key, but is a partial document | **not detected** |

Two things make that table hold. First, `_write()` writes *before* it truncates, so it can
never shrink a populated file to zero length: killed before the write, the old content is
wholly intact; killed mid-write, the result is new-prefix + old-tail, which does not parse.
Second, a file that is created is initialised with the defaults immediately, under the
lock — otherwise a transaction that declined to commit would leave a zero-length file
behind, and a legitimate zero-length file would make "present but empty" impossible to
treat as an anomaly. Both are pinned by tests, because the safety argument rests on them.

The last row is the honest gap: a partial write that happens to parse *and* carry a
recognised key cannot be detected here. In practice `_write()` emits the whole document in
one call, so a torn write yields malformed JSON — but that is a property of the write, not
something the read can verify. Closing it is what issue #79 is for.

Making the write itself crash-atomic needs a write-temp-then-rename protocol, which
interacts with the `flock` coordination here (replacing the inode invalidates locks held
on the old one) and applies equally to `facebook_state.py` and `state.py`, which share
this write pattern. It is a pre-existing weakness in all three, tracked as its own issue
rather than bolted onto this feature.

This reuses the shape of `pending_share_cleanups` deliberately rather than inventing
a third mechanism: both are unresolved external obligations that must outlive the
work that created them.

**The list is never trimmed.** It has no size or age cap, and that is deliberate:
dropping an entry would release an idempotency key while a Reel's fate is still
unknown, which is the duplicate the whole mechanism prevents. Growth is the safe
direction. What it must not be is invisible, so past `_QUARANTINE_BACKLOG_THRESHOLD`
entries every tick logs the backlog and the per-entry alerts carry the count and the
age of the oldest.

**If the Page token is removed entirely**, nothing can be asked of Instagram — but the
entries keep blocking, so that is reported rather than silent: each one alerts (on the
same daily schedule, without counting as a check that never happened) naming
`FB_PAGE_ACCESS_TOKEN` as the reason and the reconnect as the fix.

**If the Instagram cron stops entirely**, that is a different matter and worth stating
plainly. Every revocation path lives in `upload_instagram.py`: the per-attempt revoke, the
`pending_share_cleanups` drain, and the daily reminder about a link still dangling. So a
worker that dies *mid-attempt* — after creating a link and before revoking it — leaves the
link public, the obligation correctly recorded, and nothing running that would act on
either. The heartbeat does not help here and is not meant to: the worker was genuinely
alive when it created the link.

What the heartbeat's one-hour staleness window does **not** do is create this situation.
The only production caller of `drive.create_temporary_share_link()` is
`upload_instagram._process_upload`, reached only from that script's `main()` — the very
worker whose absence is in question. A link's only creator is the thing that is dead, so
"heartbeat wrongly fresh" and "a link was created" cannot both hold. During that window an
approval queues an inert job, and `upload_cleanup` retains the shared video until the
heartbeat goes stale; no public link is created. Recovering the dangling-link case needs a
revocation path that survives this worker, which is tracked separately rather than bolted
on here.

### Resolving a quarantine by hand

Normally you do nothing — the drain resolves entries by itself once Instagram answers.
If one is stuck because the container has aged out of Meta's view entirely, an operator
can settle it:

1. Read the list: `pending_publish_reconciliations` in `instagram_state.json`. Each
   entry names the `project_name`, the `container_id`, and `recorded_at`.
2. Open the client's Instagram account and look for that project's Reel around
   `recorded_at`.
3. If it IS live, nothing needs re-posting — remove the entry.
4. If it is NOT live, remove the entry; the video can then be re-approved normally.

Edit `instagram_state.json` only while no cron tick is running, and remove entries one
at a time. Removing one you have not actually checked is the one way back to a
duplicate Reel.

Note what it does **not** change: `instagram_state.mark_failed()` still discards the
whole record, mirroring `facebook_state.mark_failed()` exactly (deviation note 1).
Keeping the obligation *outside* the record is what lets the two state modules stay
aligned while Instagram still satisfies FR-011.

**Facebook has the same latent exposure, and it is not fixed here.** If
`facebook_api.upload_video()`'s response is lost, the video may be live with no record
of it, and a re-approval would post it twice.

It cannot be closed the same way **as `facebook_api.py` is currently written**: the
one-shot multipart POST at `facebook_api.py:174` knows no identifier until the response
arrives, so there is nothing to reconcile against afterwards and matching the Page's
recent videos would be a heuristic, not an authority. That is a limitation of this
implementation, **not of Meta's API** — Meta's sessionized video upload returns both an
`upload_session_id` and a `video_id` from its `start` phase, before any bytes are
transferred, which is exactly the durable pre-known handle reconciliation needs. (See
Meta's official Python Business SDK,
[`video_uploader.py`](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/video_uploader.py).)

Closing it therefore means moving Facebook onto the sessionized upload, which is out of
scope for this feature and tracked as an **urgent fast-follow** in issue #78 — the
exposure predates this change and this PR neither creates nor amplifies it, but Facebook
is live in production and a duplicate post on a client Page is irreversible.

### The account a job publishes to

The target account is the one recorded **on the job at approval time**, not whatever
`IG_BUSINESS_ACCOUNT_ID` holds when the cron happens to run. If the two disagree the
job is cancelled and the owner is alerted naming both values — neither can be shown
to be the intended one, so nothing is published. Reconfiguring a client between
approval and publish must not be able to post their video to a different account.

---

## Logs

Everything lands in `$FIELDKIT_LOG_DIR/photo-agent.log`, the same per-client file
as every other pipeline event, in the same pipe-delimited format:

| Event | Meaning |
|---|---|
| `IG_ENQUEUED` | An approval enqueued an Instagram job |
| `IG_NOWORKER` | An enqueue was **refused**: Instagram is configured but its cron is not running |
| `IG_STARTED` | An upload attempt began (with attempt number) |
| `IG_CONT_NEW` | Media container created |
| `IG_CONT_RDY` | Container finished processing, ready to publish |
| `IG_PUBLISHED` | Reel published (with post ID) |
| `IG_RECOVER` | A container was found **already published** after an interrupted run; recorded without republishing |
| `IG_UNKNOWN` | A publish was attempted and its outcome could not be established — container quarantined, key blocked |
| `IG_RESOLVED` | A quarantined container was finally confirmed as never published — quarantine lifted |
| `IG_BLOCKED` | An enqueue was **refused** because that video has an unresolved publish |
| `IG_FAILED` | One attempt failed (retryable, with error detail) |
| `IG_EXHAUSTED` | All 3 attempts consumed — terminal |
| `IG_TOKEN_EXP` | Page token invalid/expired — reconnect needed |

Two conditions are alerted to the admin over Telegram but not given their own log
event: a share link that could not be revoked (see above), and a publish whose
permalink lookup failed (the Reel is live; only the link is missing).

No token value or PII is ever written to the log. No logging function accepts a
token argument, and `_safe_error()` redacts credentials out of arbitrary exception
text before it is written — see `tools/redaction.py`.

State lives in `<FIELDKIT_DATA_DIR>/photo-agent/instagram_state.json` — a separate
file from `facebook_state.json`. Two lists in it are worth knowing by name when
auditing:

- `pending_share_cleanups` — Drive links that may still be public
- `pending_publish_reconciliations` — publishes whose outcome is unknown. A
  non-empty list means a Reel **may** be live on the account with nothing recording
  it, and that its idempotency key is blocked against re-approval until Instagram
  answers. Both empty is the healthy state.

---

## A note on Hermes skills

**Neither of Feature 005's scripts gets a `SKILL.md`, deliberately.** This matches
how Feature 003's equivalents are handled, and it is why their usage is
documented here instead:

- `upload_instagram.py` is cron-only, exactly like `upload_facebook.py` — which
  has no entry under `platform/photo-agent/skills/` either. The owner never
  invokes it; cron does.
- `check_instagram_connection.py` is a one-time **admin** CLI, like
  `generate_auth_link.py` — also not a skill. The business owner never runs it.

The only Hermes skills in the photo-agent are the ones an owner actually types:
`process-photos`, `photo-approve`, `photo-reject`. Adding a skill for either
script here would expose an operator tool as an owner-facing command.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `check_instagram_connection.py` exits 3, "No Instagram account is linked" | Prerequisite 3 not done — link the account to the Page in Meta's settings |
| Exits 3, "is a PERSONAL account" | Prerequisite 2 not done — convert to Business/Creator in the Instagram app |
| Exits 1, "token is invalid or expired" | Re-run `generate_auth_link.py --instagram` to reconnect the Page |
| Publishing fails with a permissions error though the account is linked | The Page token lacks `instagram_basic` / `instagram_content_publish` — do Step 0 |
| Step 0's OAuth dialog says "Invalid Scopes" | The Meta app has not enabled the two Instagram permissions — prerequisite 4 |
| Nothing happens on approval; no `IG_ENQUEUED` in the log | `IG_BUSINESS_ACCOUNT_ID` not set for this client — run step 1 |
| `IG_FAILED` with "did not finish processing" | Container stuck past 300s; retried automatically, often a large or oddly-encoded video |
| `IG_TOKEN_EXP` | Page token expired — the Facebook upload will be failing too; reconnect once, fixes both |
| Alert naming a Drive file that "may still be publicly reachable" | A revoke failed; FieldKit keeps retrying each tick and re-alerts daily. Check `pending_share_cleanups` in `instagram_state.json`; to fix it now, remove the file's "Anyone with the link" permission in Drive |
| The same share-link alert arriving daily | Cleanup is still failing after many attempts. The `attempts` count in the alert says how many. Resolve it manually in Drive — the reminder stops as soon as the revoke succeeds |
| Confirmation says "could not fetch the post link" | The Reel published, but the permalink lookup failed. Check the account directly; no retry is attempted since the post is already live |
| Local video still on disk after a publish | Expected while the other platform's job for that approval is still pending — the last one to resolve deletes it |
| Facebook posted but Instagram didn't (or vice versa) | Expected and by design — the two are independent (FR-013). Check the log for that platform's own events |
