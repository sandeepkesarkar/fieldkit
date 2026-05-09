# Clients

Each client gets their own directory here — a complete, isolated spec-kit instance with their own constitution, feature specs, and implementation code.

## Structure

```
clients/
├── _template/          # Start here — copy this for every new client
└── <client-name>/      # One directory per client
    ├── README.md
    ├── .specify/
    │   ├── memory/
    │   │   └── constitution.md   # Governing principles for this client
    │   └── specs/
    │       └── <NNN>-<feature>/
    │           └── spec.md
    └── src/             # Implementation code (populated during build phase)
```

## Starting a New Client

```bash
cp -r clients/_template clients/<your-client-name>
```

Then follow the onboarding steps in [CONTRIBUTING.md](../CONTRIBUTING.md).

## A Note on Client Privacy

Client names, locations, and implementation details are kept out of this public repo. Each client's production implementation lives in a separate private repository. What lives here in the public repo is the `_template` — the starting point every client implementation is built from.

## The `_demo` Client

`clients/_demo/` is a reference implementation using fake/placeholder data. It exists to:
- Show the full FieldKit workflow end-to-end (constitution → spec → plan → code)
- Serve as a development and testing sandbox
- Illustrate how to configure platform-level engines for a specific client

It is not a real engagement. All credentials and sensitive values in `_demo` are clearly marked `[DEMO]` and never committed.
