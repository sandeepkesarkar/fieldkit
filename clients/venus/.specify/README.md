# Venus — Spec-Kit Directory

This directory holds all spec-kit artifacts for venus's implementation.

## Structure

```
.specify/
├── constitution.md         ← client principles (this file's sibling)
├── {NNN}-{feature-name}/   ← one directory per feature (none yet — see below)
│   ├── spec.md             ← requirements (/speckit-specify)
│   ├── clarify.md          ← Q&A (/speckit-clarify)
│   ├── plan.md             ← technical plan (/speckit-plan)
│   ├── sequence-diagram.md ← Mermaid diagram (/speckit-plan)
│   ├── tasks.md            ← task breakdown (/speckit-tasks)
│   └── features/
│       └── *.feature       ← Gherkin acceptance tests (/speckit-tasks)
└── README.md               ← this file
```

## Spec-Kit Workflow

```
/speckit-constitution  →  Establish client principles
/speckit-specify       →  Write feature spec
/speckit-clarify       →  Resolve ambiguities
/speckit-plan          →  Technical plan + sequence diagram
/speckit-tasks         →  Task breakdown + Gherkin features
/speckit-implement     →  Build with AI assistance
```

## Current Features

| ID | Feature | Status |
|----|---------|--------|
| — | *(scaffolded directly from issue #12 — the photo-approval pipeline itself is platform-owned, spec'd under `platform/.specify/003-hermes-runtime/`; no venus-local feature spec exists yet)* | — |

---

*Specs are technology-agnostic — they define what and why, not how.*
