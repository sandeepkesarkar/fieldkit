# Demo Client — Spec-Kit Directory

This directory holds all spec-kit artifacts for the Demo Client reference implementation.

## Structure

```
.specify/
├── constitution.md         ← client principles (created by /speckit-constitution)
├── feature.json            ← active feature pointer (managed by spec-kit)
├── {NNN}-{feature-name}/   ← one directory per feature
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
| 001 | Email Intake | Spec ✅ |
| 002 | Photo Video Agent | Spec ✅ / Clarify ✅ / Plan ✅ / Tasks ✅ |

---

*Specs are technology-agnostic — they define what and why, not how.*
