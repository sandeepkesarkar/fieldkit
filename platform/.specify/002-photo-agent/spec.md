# Feature Specification: Platform Photo-Agent

**Feature Branch**: `001-platform-photo-agent`

**Created**: 2026-07-01

**Status**: Draft

**Input**: Migrate the photo-agent pipeline from `clients/_demo` to `platform/photo-agent` so all FieldKit clients can share the pipeline without duplicating code.

---

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Existing _demo client continues to work after migration (Priority: P1)

The _demo client continues to operate identically after migration — same commands, same Telegram flows, same Drive and Facebook behavior — with all pipeline code now running from the platform layer.

**Why this priority**: Zero regression is the hard gate. No migration ships if the existing client breaks.

**Independent Test**: Run the full existing test suite against the migrated codebase; all tests pass. Invoke `/process_photos`, `/check_approval`, and `/upload_facebook` manually and observe identical behavior to pre-migration.

**Acceptance Scenarios**:

1. **Given** the migration is complete, **When** `/process_photos kitchen_remodel` is invoked, **Then** the platform script loads `_demo` config, runs the Drive → FFmpeg → Telegram approval flow, and produces the same outcome as before migration.
2. **Given** state files exist at the old repo-root `data/photo-agent/` location, **When** the operator performs the one-time data migration step, **Then** state is accessible at the new per-client location and all scripts read it correctly.
3. **Given** the migration is complete, **When** all unit tests run from `platform/photo-agent/tests/`, **Then** all 363 existing tests pass without modification.

---

### User Story 2 - A new client activates the photo-agent with config only (Priority: P2)

A developer onboarding a new client enables the full photo-agent pipeline by creating a single `.env` file in the client folder and setting `CLIENT_NAME` in the root config — without touching any platform code.

**Why this priority**: This is the primary motivation for the migration — eliminating copy-paste across clients.

**Independent Test**: Create `clients/_acme/src/photo-agent/.env` with different credentials. Set `CLIENT_NAME=_acme` in the root `.env`. Run the photo-agent pipeline and confirm it uses `_acme` credentials and writes state to `clients/_acme/data/`.

**Acceptance Scenarios**:

1. **Given** `CLIENT_NAME=_acme` in root `.env` and `clients/_acme/src/photo-agent/.env` exists with valid config, **When** any photo-agent platform script runs, **Then** it loads `_acme` credentials and writes state/logs to `_acme`'s directories.
2. **Given** no `CLIENT_NAME` is set in root `.env`, **When** any platform script runs, **Then** it exits within 1 second with a clear error message naming the missing variable.
3. **Given** two clients on two different machines each with their own root `.env`, **When** each runs the photo-agent, **Then** each operates against its own config, state, and logs with no cross-contamination.

---

### User Story 3 - SKILL files require no per-client edits (Priority: P3)

Platform SKILL files (`SKILL_process_photos.md` etc.) work for any client without modification — the client identity is resolved at runtime via the root config, not hard-coded in the skill definitions.

**Why this priority**: Eliminates ongoing SKILL maintenance burden as new clients are added.

**Independent Test**: Use the same unmodified `SKILL_process_photos.md` from `platform/photo-agent/` while switching `CLIENT_NAME` between two clients; confirm each invocation uses the correct client config.

**Acceptance Scenarios**:

1. **Given** SKILL files reside in `platform/photo-agent/`, **When** a new client is onboarded, **Then** no SKILL file needs to be copied, edited, or overridden for standard pipeline operation.
2. **Given** `CLIENT_NAME` is set in root `.env`, **When** a SKILL invokes a platform script, **Then** the correct client config is loaded with no client-specific path in the SKILL definition.

---

### Edge Cases

- What happens when `CLIENT_NAME` is set but the referenced client `.env` does not exist? → Script exits with a clear error naming the missing file path.
- What happens when `FIELDKIT_DATA_DIR` or `FIELDKIT_LOG_DIR` is not set in the client `.env`? → Script exits with a named error rather than silently writing to an incorrect location.
- What happens when two concurrent runs target the same client? → File locking via `run.lock` is unchanged; the existing single-concurrency mechanism continues to apply per client.
- What happens if the old `data/photo-agent/` location still has state files after migration? → Platform scripts read only the new location (from `FIELDKIT_DATA_DIR`). Old files are inert — neither read nor deleted automatically.

---

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: All photo-agent pipeline scripts MUST reside in `platform/photo-agent/scripts/` and be shared across all clients without modification.
- **FR-002**: All photo-agent tool modules MUST reside in `platform/photo-agent/tools/` and be shared across all clients without modification.
- **FR-003**: All photo-agent unit and integration tests MUST reside in `platform/photo-agent/tests/` and pass without client-specific overrides.
- **FR-004**: All photo-agent SKILL files MUST reside in `platform/photo-agent/` and contain no client-specific paths or credentials.
- **FR-005**: Each client's photo-agent secrets and path config MUST reside exclusively in `clients/{name}/src/photo-agent/.env`; no secrets or paths are stored in platform code.
- **FR-006**: Platform scripts MUST load config in two ordered steps: (1) repo-root `.env` for `CLIENT_NAME` and `FIELDKIT_ROOT`, (2) the resolved client `.env` for secrets and path overrides, with client values taking precedence.
- **FR-007**: `CLIENT_NAME` MUST be defined in the repo-root `.env` and MUST resolve to an existing client directory; platform scripts MUST exit with a named error if either condition is not met.
- **FR-008**: `FIELDKIT_DATA_DIR` and `FIELDKIT_LOG_DIR` MUST be explicitly set in each client's `.env`; platform tools MUST exit with a named error if either is unset — no silent fallback to a hardcoded path.
- **FR-009**: Each client's runtime state files (`state.json`, `facebook_state.json`, `run.lock`) MUST be written to and read from the directory identified by `FIELDKIT_DATA_DIR`.
- **FR-010**: Each client's activity log MUST be written to the directory identified by `FIELDKIT_LOG_DIR`.
- **FR-011**: The `_demo` client `.env` MUST be updated to include `FIELDKIT_DATA_DIR` pointing to `clients/_demo/data/` and `FIELDKIT_LOG_DIR` pointing to `clients/_demo/logs/`.
- **FR-012**: Existing runtime state from the repo-root `data/photo-agent/` MUST be migrated to `clients/_demo/data/photo-agent/` as a documented one-time operator step before the new paths go live.
- **FR-013**: The repo-root `.env.example` MUST document all root-level variables (`CLIENT_NAME`, `FIELDKIT_ROOT`) with descriptions.
- **FR-014**: `platform/photo-agent/.env.example` MUST document all variables required in a client `.env`, including `FIELDKIT_DATA_DIR` and `FIELDKIT_LOG_DIR`, with descriptions.

### Key Entities

- **Root config** (`fieldkit/.env`): Machine-level identity; holds `CLIENT_NAME` and `FIELDKIT_ROOT`; no secrets; `.env.example` is safe to commit.
- **Client config** (`clients/{name}/src/photo-agent/.env`): All secrets and path overrides for one client; never committed.
- **Platform photo-agent** (`platform/photo-agent/`): Shared pipeline code — scripts, tools, tests, SKILL files, docs; no client-specific content.
- **Client photo-agent folder** (`clients/{name}/src/photo-agent/`): Config files only after migration (`.env`, `.env.example`); no Python source.

---

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: All existing `_demo` photo-agent tests (363 at migration time) pass without modification after migration.
- **SC-002**: A second client can be made operational with the photo-agent pipeline by creating one `.env` file and one root config change — no code changes required.
- **SC-003**: `clients/_demo/src/photo-agent/` contains no Python source files after migration (`.env` and `.env.example` only).
- **SC-004**: `platform/photo-agent/SKILL_*.md` files contain no client-specific paths or credentials.
- **SC-005**: Any platform script invoked without `CLIENT_NAME` set produces an actionable error message within 1 second.
- **SC-006**: Any platform script invoked without `FIELDKIT_DATA_DIR` or `FIELDKIT_LOG_DIR` set produces an actionable error message within 1 second.

---

## Assumptions

- `_demo` is the only active client at migration time; no other clients need to be onboarded as part of this feature.
- The Instagram upload feature (`_demo` Feature 005) is not yet implemented; this migration lands first so Instagram inherits the platform structure from day one.
- `FIELDKIT_ROOT` is the absolute path to the repo root on the host machine; this value varies per machine and is set once in `fieldkit/.env` during initial setup.
- Existing state at the repo-root `data/photo-agent/` is migrated manually by the operator before switching `FIELDKIT_DATA_DIR`; no automated migration script is required for the single-client case.
- All existing tests already override data/log paths via environment variables or temp directories and will remain correct after migration.
- Facebook setup docs (`docs/facebook/`) move to `platform/photo-agent/docs/` as they describe platform-level OAuth setup, not `_demo`-specific configuration.
