<!-- SPECKIT START -->
## FieldKit — Multi-Unit Spec-Kit Configuration

This is the FieldKit mono-repo. It contains multiple specifiable units:
- **`platform/`** — Shared infrastructure (email agent, cron workers, etc.)
- **`clients/{name}/`** — Client implementations (currently `_demo`, `_construction_co`, `_template`)

### Before any spec-kit command

When running any `/speckit-*` command, first determine which unit is targeted:

1. Ask the user: "Which unit is this for? (`platform` or `clients/{name}`)"
2. Scan the target unit's `.specify/` directory for existing `NNN-*` directories to determine the next sequential number
3. Set `SPECIFY_FEATURE_DIRECTORY` to `{unit}/.specify/{NNN}-{short-name}`

Example: for a new photo agent spec in the demo client →
`SPECIFY_FEATURE_DIRECTORY=clients/_demo/.specify/002-photo-video-agent`

### Feature directory layout

```
{unit}/.specify/
├── constitution.md        ← client-specific principles (created by /speckit-constitution)
├── {NNN}-{feature}/       ← one directory per feature
│   ├── spec.md
│   ├── clarify.md
│   ├── plan.md
│   ├── sequence-diagram.md
│   ├── tasks.md
│   └── features/
│       └── *.feature
└── feature.json           ← active feature pointer (written by /speckit-specify)
```

### Required artifacts per feature (FieldKit adds to spec-kit defaults)

- **`sequence-diagram.md`** — Mermaid sequenceDiagram showing happy-path flow between actors. Created by `/speckit-plan` in addition to `plan.md`.
- **`features/*.feature`** — Gherkin acceptance test files. Created by `/speckit-tasks` in a `features/` subdirectory — one file per major user story or behavior cluster.

### Constitution context

- Root `.specify/memory/constitution.md` — FieldKit framework principles (privacy, HITL, budget governance) that apply to ALL client work
- `{client}/.specify/constitution.md` — Client-specific principles (created per client by `/speckit-constitution`)
### Active feature plan

**Platform Feature 002 — Photo-Agent Migration: IMPLEMENTATION COMPLETE (T001–T048).**
Tasks: [`platform/.specify/002-photo-agent/tasks.md`](platform/.specify/002-photo-agent/tasks.md)
Branch: `001-platform-photo-agent`
Status: `clients/_construction_co` scaffolded (credentials pending). Remaining: T040–T042 (live two-client isolation run), T050 (adversarial review). Test counts on `main` as of 2026-10-02: `platform/photo-agent` 1660 passed / 2 skipped, `platform/email-agent` 84 passed (run each package's `tests/` separately — a bare `pytest` from the repo root errors collecting `platform/photo-agent/scripts/run_e2e_test.py`).

Permanent clients:
- `_demo` — full pipeline (video → Telegram approval → Facebook)
- `_construction_co` — scoped pipeline (video → Telegram approval only, no Facebook)

**_demo Feature 005 — Instagram Video Upload: MERGED to `main` (T000–T021).**
Spec/tasks: [`clients/_demo/.specify/005-instagram-video-upload/`](clients/_demo/.specify/005-instagram-video-upload/)
**PR**: https://github.com/sandeepkesarkar/fieldkit/pull/72 (merged 2026-09-25)
Status: Implemented by claude_code, cross-reviewed by codex (different vendor); all blocking findings fixed before merge. Orchestration record: `.polly/registry.json`. Post-merge hardening landed via #85/#87/#90/#92/#94 (Facebook sessioned upload #78, Instagram quarantine resolution #88, watermark escaping #86, installer fixes #81/#89). Open follow-up: PR #95 (Instagram share-link cleanup drained from both cron workers, #80). Platform 002 closeout (T040–T042, T050) remains outstanding.
<!-- SPECKIT END -->
