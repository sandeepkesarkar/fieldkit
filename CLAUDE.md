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
Status: 368 tests passing. `clients/_construction_co` scaffolded (credentials pending). Remaining: T040–T042 (live two-client isolation run), T050 (adversarial review).

Permanent clients:
- `_demo` — full pipeline (video → Telegram approval → Facebook)
- `_construction_co` — scoped pipeline (video → Telegram approval only, no Facebook)

**_demo Feature 005 — Instagram Video Upload: IMPLEMENTATION COMPLETE (T000–T021).**
Spec: [`clients/_demo/.specify/005-instagram-video-upload/spec.md`](clients/_demo/.specify/005-instagram-video-upload/spec.md)
Plan: [`clients/_demo/.specify/005-instagram-video-upload/plan.md`](clients/_demo/.specify/005-instagram-video-upload/plan.md)
Branch: `002-instagram-video-upload`
Tasks: [`clients/_demo/.specify/005-instagram-video-upload/tasks.md`](clients/_demo/.specify/005-instagram-video-upload/tasks.md)
Status: All 22 tasks complete; 840 tests collected, 821 passing (up from 580/561). Started ahead of the original "next after Platform 002 adversarial review passes" ordering, per explicit human decision on 2026-08-31. Platform 002 closeout (T040–T042 live two-client isolation run, T050 adversarial review) remains outstanding and is tracked separately — not a blocker for Feature 005's own implementation.

Remaining for Feature 005: live run against a real linked Instagram account (`check_instagram_connection.py` + one real approval), and cron installation for `upload_instagram.py` — both deployment-time steps, not repo changes.
<!-- SPECKIT END -->
