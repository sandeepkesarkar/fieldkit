# FieldKit

A spec-first framework for building AI-powered automation systems for small service businesses — self-hosted, cost-controlled, and human-in-the-loop by design.

---

## What is FieldKit?

Small service businesses — contractors, bakers, dance teachers, music teachers, cleaners, landscapers, salons, plumbers — are underserved by off-the-shelf software. Their needs are specific, their budgets are tight, and their time is scarce.

FieldKit is a framework for building tailored automation systems for these businesses. Not a SaaS product. Not a one-size-fits-all tool. A structured approach to rapidly delivering custom solutions, built on proven patterns, that each client fully owns.

Each FieldKit implementation gives a business:

- **Social media automation** — before/after project posts, privacy-scrubbed, human-approved before anything goes live

Every feature is governed by the same principles: privacy-first, cost-controlled, and no AI-generated content goes public without a human approving it.

---

## How It Works

FieldKit combines three things:

**1. spec-kit** — a spec-driven development methodology. Requirements are fully specified before any code is written. Technology decisions come after specs are validated, not before.

**2. OpenClaw** — a self-hosted, open-source AI agent runtime. Runs on a dedicated Mac Mini at the client's location. No per-API-call cloud costs. No vendor lock-in.

**3. This framework** — the structure, patterns, governance docs, and client template that tie everything together.

The result: a custom system the client fully owns, built faster than starting from scratch, on infrastructure they control.

---

## Prerequisites

Before using FieldKit, you need the infrastructure layer in place:

→ **[mac-mini-dev-setup](https://github.com/sandeepkesarkar/mac-mini-dev-setup)** — step-by-step guide to configuring a Mac Mini as a dedicated AI development and deployment machine, including OpenClaw installation.

---

## Repository Structure

```
fieldkit/
├── .specify/specs/          # Platform-wide governance docs
│   ├── framework-philosophy.md
│   ├── extraction-plan.md
│   └── development-log.md
│
├── platform/                # Shared engines (extracted after N≥2 clients)
│
├── clients/                 # One directory per client
│   └── _template/           # Starter kit — copy this for every new client
│       ├── .specify/
│       │   ├── memory/
│       │   │   └── constitution.md   # Governing principles template
│       │   └── specs/
│       └── src/
│
└── updates/                 # Open development — weekly build-in-public posts
```

---

## Core Principles

- **Spec-first** — requirements fully defined before any code is written
- **Privacy-first** — customer data protected at every layer; metadata stripped from all media
- **Human-in-the-loop** — no AI-generated content goes public without admin approval
- **Cost-conscious** — hard daily budget limits enforced automatically
- **Client ownership** — each client gets their own isolated codebase they fully own
- **Incremental delivery** — one feature shipped and validated before the next begins

See [`.specify/specs/framework-philosophy.md`](.specify/specs/framework-philosophy.md) for the full philosophy.

---

## Getting Started

### Onboarding a New Client

1. **Set up infrastructure** — follow [mac-mini-dev-setup](https://github.com/sandeepkesarkar/mac-mini-dev-setup)
2. **Copy the client template**
   ```bash
   cp -r clients/_template clients/<your-client-name>
   ```
3. **Write the constitution** — fill in `clients/<your-client-name>/.specify/memory/constitution.md`
4. **Write feature specs** — use the spec-kit workflow (see below)
5. **Run clarification** — resolve ambiguities before planning
6. **Plan and build** — technology decisions happen here, after specs are solid

### The spec-kit Workflow

```
/speckit.constitution  →  Establish governing principles
/speckit.specify       →  Write feature specifications
/speckit.clarify       →  Resolve ambiguities before planning
/speckit.plan          →  Generate technical implementation plan
/speckit.tasks         →  Break down into actionable tasks
/speckit.implement     →  Build with AI assistance
```

Specifications are technology-agnostic — they define *what* and *why*, not *how*. Tech stack decisions happen in the planning phase, after specs are complete and validated.

---

## Built in Public

FieldKit is developed openly. Every week, a post goes out covering the real journey — decisions made, things that didn't work, patterns discovered.

Follow the build: [`updates/`](updates/)

LinkedIn: [Sandeep Kesarkar](https://www.linkedin.com/in/sandeepkesarkar)

---

## Methodology

FieldKit uses [spec-kit](https://github.com/github/spec-kit) for spec-driven development and [OpenClaw](https://github.com/openclaw/openclaw) as the self-hosted AI agent runtime.

---

## License

MIT — see [LICENSE](LICENSE)

---

*Built in public · Powered by [OpenClaw](https://github.com/openclaw/openclaw) · Methodology: [spec-kit](https://github.com/github/spec-kit)*
