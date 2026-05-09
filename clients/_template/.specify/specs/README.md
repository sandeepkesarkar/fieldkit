# [Client Name] — Feature Specs

This directory contains feature specifications for [Client Name]'s implementation.

## Spec-Kit Workflow

```
/speckit.constitution  →  Establish governing principles (done — see ../memory/)
/speckit.specify       →  Write feature spec         ← start here
/speckit.clarify       →  Resolve ambiguities
/speckit.plan          →  Technical implementation plan
/speckit.tasks         →  Task breakdown
/speckit.implement     →  Build with AI assistance
```

## Naming Convention

Each feature gets its own directory:

```
specs/
├── 001-<feature-name>/
│   ├── spec.md      # Feature specification (tech-agnostic)
│   ├── plan.md      # Technical plan (added after /speckit.plan)
│   └── tasks.md     # Task breakdown (added after /speckit.tasks)
└── 002-<feature-name>/
```

Number features in the order they'll be built. Phase determines priority.

## Current Features

| ID | Feature | Phase | Status |
|----|---------|-------|--------|
| — | *(no specs yet)* | — | — |

---

*Specs are technology-agnostic — they define what and why, not how.*
*Technology decisions happen in the planning phase, after specs are validated.*
