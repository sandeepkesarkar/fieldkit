<!-- SPECKIT START -->
## FieldKit — Multi-Unit Spec-Kit Configuration

This is the FieldKit mono-repo. It contains multiple specifiable units:
- **`platform/`** — Shared infrastructure (email agent, cron workers, etc.)
- **`clients/{name}/`** — Client implementations (currently `_demo`, `_template`)

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

**Feature 003 — Facebook Video Upload**
Plan: [`clients/_demo/.specify/003-facebook-upload/plan.md`](clients/_demo/.specify/003-facebook-upload/plan.md)
Branch: `001-upload-facebook-video`
Status: Planning complete — ready for `/speckit-tasks`
<!-- SPECKIT END -->
