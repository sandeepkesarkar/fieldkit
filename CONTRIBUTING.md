# Contributing to FieldKit

Welcome to FieldKit. This guide covers how to work in this repo — branching, commits, PRs, and onboarding new clients.

---

## Branching Strategy

We use a simple branch structure that scales cleanly as clients grow.

### Branch Types

| Branch | Purpose | Example |
|--------|---------|---------|
| `main` | Stable, production-ready code only | — |
| `client/<name>/<description>` | All work for a specific client | `client/main-street-plumbing/social-media-spec` |
| `platform/<description>` | Shared platform engine work | `platform/cost-tracking` |
| `chore/<description>` | Docs, config, housekeeping | `chore/update-gitignore` |
| `fix/<description>` | Bug fixes | `fix/photo-upload-error` |

### Rules

- **Never commit directly to `main`** — all changes go through a PR
- Branch off `main`, not off other feature branches
- Keep branches short-lived — merge and delete when done
- One concern per branch — don't mix client work with platform work

### Example Workflow

```bash
# Start new work for a client
git checkout main && git pull
git checkout -b client/main-street-plumbing/whatsapp-webhook

# ... do work ...

git add <files>
git commit -m "feat(main-street-plumbing): add webhook handler for incoming messages"
git push -u origin client/main-street-plumbing/whatsapp-webhook

# Open a PR → review → merge → delete branch
```

---

## Commit Message Convention

We follow a simple convention based on [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short description>
```

### Types

| Type | When to use |
|------|-------------|
| `feat` | New feature or capability |
| `fix` | Bug fix |
| `chore` | Maintenance, config, dependencies |
| `docs` | Documentation only |
| `refactor` | Code change with no behaviour change |
| `test` | Adding or updating tests |
| `spec` | Spec or constitution changes |

### Scope

Use the client name or area:

- `<client-name>` — client-specific work
- `platform` — shared platform code
- `framework` — framework docs/specs

### Examples

```
feat(main-street-plumbing): add Facebook Graph API posting
fix(platform): handle Gmail API rate limit retries correctly
chore: update .gitignore to exclude Python venv directories
docs: add CONTRIBUTING guide and branching strategy
```

---

## Pull Requests

### Before Opening a PR

- [ ] Branch is up to date with `main`
- [ ] Code runs locally without errors
- [ ] Unit tests written and passing
- [ ] Integration tests written and passing
- [ ] Relevant spec updated if behaviour changed
- [ ] No secrets or credentials committed

### PR Title

Follow the same convention as commit messages:
```
feat(client-name): add Instagram posting for approved social media drafts
```

### PR Description

Include:
- **What** changed and **why**
- Any decisions made that weren't obvious
- Link to relevant spec section if applicable
- Testing notes (how to verify it works)

### Review

- At least one review required before merging
- Address all comments before merging
- Squash commits on merge to keep `main` history clean

---

## Onboarding a New Client

When FieldKit takes on a new client, follow these steps:

### 1. Copy the client template

```bash
CLIENT=<client-slug>   # e.g. "main-street-plumbing"

cp -r clients/_template clients/$CLIENT
```

### 2. Create a branch for the onboarding work

```bash
git checkout -b client/$CLIENT/initial-setup
```

### 3. Write the constitution

Fill in `clients/$CLIENT/.specify/memory/constitution.md` following the guided prompts in the template. Key sections:
- Core values
- Feature-specific principles
- Technical constraints
- Decision-making framework

### 4. Write feature specs

For each feature, create:
```
clients/$CLIENT/.specify/specs/<NNN>-<feature-name>/spec.md
```

Run `/speckit.specify` to guide the spec-writing process.

### 5. Create a client README

Update `clients/$CLIENT/README.md` with:
- Client overview
- Feature table with status
- Link to constitution
- Development workflow

### 6. Open a PR for review before starting implementation

---

## Testing

FieldKit follows test-driven development. Tests are written before or alongside implementation — never retrofitted at the end.

### Requirements

Every feature implementation must include:

- **Unit tests** — test individual functions and components in isolation; mock external dependencies (Gmail, Telegram)
- **Integration tests** — test the end-to-end flow of the feature against real or realistic dependencies

A feature is not considered complete until both levels of tests pass. No code merges to `main` without passing tests.

### What to test

| Level | Tests what | Dependencies |
|-------|-----------|-------------|
| Unit | Individual functions, logic, edge cases | Mocked |
| Integration | Full feature flow end-to-end | Real or realistic test doubles |

### Tests as documentation

Tests describe expected behaviour. Write test names that read as specifications:

```
test_valid_email_from_allowlisted_sender_triggers_telegram_ack
test_unknown_sender_is_silently_rejected_and_logged
test_telegram_failure_falls_back_to_email_reply
```

### Tests travel with the client

When a client's system is handed off, the full test suite is included. This lets the client (or any future developer they hire) verify the system still works after changes.

---

## Working with Specs

Specs live in `.specify/specs/` at the appropriate level. Follow the spec-kit workflow:

```
/speckit.constitution  →  Write governing principles
/speckit.specify       →  Write feature spec
/speckit.clarify       →  Resolve ambiguities
/speckit.plan          →  Technical implementation plan
/speckit.tasks         →  Task breakdown
/speckit.implement     →  Build with AI assistance
```

**Key rule:** Specs are updated in the same PR as the code they describe. If you change behaviour, update the spec.

---

## Publishing Weekly Updates

FieldKit is built in public. Every week, a post goes out on LinkedIn covering the real journey — decisions made, things learned, patterns discovered.

### How updates work

1. Write the LinkedIn post as a markdown file in `updates/`
2. Use the naming convention: `YYYY-MM-DD.md` (date of publication)
3. Use `updates/_template.md` as your starting point
4. Post to LinkedIn; add the post URL to the bottom of the markdown file

### Guidelines

- Write in first person — this is a personal journey, not a press release
- Be honest about what didn't work, not just what did
- One post per week, every week
- Soft CTA at the end — link to the repo or the next post topic

---

## Security & Secrets

- **Never commit secrets, API keys, credentials, or `.env` files**
- All credentials go in environment variables or a secrets manager
- The `.gitignore` excludes `.env` files — double-check before committing
- If you accidentally commit a secret, rotate it immediately

---

## Project Structure Reference

```
fieldkit/
├── .specify/specs/          # Platform-wide governance docs
├── platform/                # Shared engines (empty until N≥2 clients prove patterns)
├── clients/
│   ├── _template/           # Starter kit for new clients
│   └── <client-name>/
│       ├── .specify/
│       │   ├── memory/      # Constitution
│       │   └── specs/       # Feature specs
│       └── src/             # Implementation code
├── updates/                 # Weekly LinkedIn posts
├── CONTRIBUTING.md          # This file
├── README.md
└── LICENSE
```
