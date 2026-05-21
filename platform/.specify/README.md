# Platform — Spec-Kit Directory

This directory holds all spec-kit artifacts for FieldKit's shared platform infrastructure.

## Structure

```
.specify/
├── memory/constitution.md  ← framework-level constitution (read by /speckit-plan)
├── feature.json            ← active feature pointer (managed by spec-kit)
├── {NNN}-{feature-name}/   ← one directory per platform feature
│   ├── spec.md
│   ├── clarify.md
│   ├── plan.md
│   ├── sequence-diagram.md
│   ├── tasks.md
│   └── features/
│       └── *.feature
└── README.md               ← this file
```

## Current Features

| ID | Feature | Status |
|----|---------|--------|
| 001 | Email Agent | Complete ✅ (28 tests, live on Mac Mini) |

---

*Platform features must appear in N≥2 client implementations before extraction.*
*Exception: foundational infrastructure (email agent) may be extracted earlier.*
