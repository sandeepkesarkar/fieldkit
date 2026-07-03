# Tasks: Platform Photo-Agent Migration

**Input**: `platform/.specify/002-photo-agent/plan.md` + `spec.md`
**Branch**: `001-platform-photo-agent`
**Total tasks**: 50

## Format: `[ID] [P?] [Story?] Description`

- **[P]**: Can run in parallel with other [P] tasks in the same phase
- **[Story]**: Which user story this task belongs to (US1 / US2 / US3)

---

## Phase 1: Setup

**Purpose**: Create the platform directory structure that everything migrates into.

- [x] T001 Create `platform/photo-agent/scripts/`, `tools/`, `tests/`, `docs/facebook/` directories
- [x] T002 Create `clients/_demo/data/photo-agent/` directory (destination for state files)
- [x] T003 Create `clients/_demo/logs/` directory (destination for log file)

---

## Phase 2: Foundational — Root Config

**Purpose**: Machine-identity config that every platform script reads first. Must exist before any script is tested.

- [x] T004 Create `fieldkit/.env.example` documenting `CLIENT_NAME` and `FIELDKIT_ROOT` with descriptions
- [x] T005 Create `fieldkit/.env` with `CLIENT_NAME=_demo` and `FIELDKIT_ROOT=<absolute-repo-path>` for this machine

**Checkpoint**: Root config in place — platform scripts can now resolve the active client.

---

## Phase 3: User Story 1 — _demo client works unchanged (Priority: P1) 🎯 MVP

**Goal**: All pipeline code lives in `platform/photo-agent/`; `_demo` works identically to pre-migration; all 363 tests pass.

**Independent Test**: Run `pytest platform/photo-agent/tests/` — all 363 tests green. Invoke `/process_photos` manually and observe identical Telegram + Drive behavior.

### Gherkin acceptance tests

- [x] T006 [P] [US1] Write `platform/.specify/002-photo-agent/features/migration.feature` — scenarios: _demo pipeline works post-migration; old data/photo-agent/ state is readable from new path

### Tools migration

- [x] T007 [P] [US1] Copy `clients/_demo/src/photo-agent/tools/drive.py` → `platform/photo-agent/tools/drive.py` (verbatim — no path-counting code)
- [x] T008 [P] [US1] Copy `clients/_demo/src/photo-agent/tools/telegram_api.py` → `platform/photo-agent/tools/telegram_api.py` (verbatim)
- [x] T009 [P] [US1] Copy `clients/_demo/src/photo-agent/tools/facebook_api.py` → `platform/photo-agent/tools/facebook_api.py` (verbatim)
- [x] T010 [P] [US1] Copy `clients/_demo/src/photo-agent/tools/video_generator.py` → `platform/photo-agent/tools/video_generator.py` (verbatim)
- [x] T011 [US1] Copy `clients/_demo/src/photo-agent/tools/state.py` → `platform/photo-agent/tools/state.py`; remove `Path(__file__).parents[5]` fallback from `DATA_DIR`; raise `RuntimeError("FIELDKIT_DATA_DIR is not set — add it to your client .env")` if env var is unset
- [x] T012 [US1] Copy `clients/_demo/src/photo-agent/tools/logger.py` → `platform/photo-agent/tools/logger.py`; remove `Path(__file__).parents[5]` fallback from `LOG_DIR`; raise `RuntimeError("FIELDKIT_LOG_DIR is not set — add it to your client .env")` if env var is unset
- [x] T013 [US1] Copy `clients/_demo/src/photo-agent/tools/facebook_state.py` → `platform/photo-agent/tools/facebook_state.py`; remove `Path(__file__).parents[5]` fallback from `DATA_DIR`; reuse `FIELDKIT_DATA_DIR` env var (same error as T011)
- [x] T014 [US1] Copy `clients/_demo/src/photo-agent/tools/facebook_logger.py` → `platform/photo-agent/tools/facebook_logger.py`; remove `Path(__file__).parents[5]` fallback from `LOG_DIR`; reuse `FIELDKIT_LOG_DIR` env var (same error as T012)
- [x] T015 [P] [US1] Copy `clients/_demo/src/photo-agent/tools/__init__.py` → `platform/photo-agent/tools/__init__.py` (verbatim)

### Scripts migration — 2-step env loading

Replace `load_dotenv(Path(__file__).parents[1] / ".env")` in every script with:
```python
_ROOT = Path(os.environ.get("FIELDKIT_ROOT", str(Path(__file__).parents[3])))
load_dotenv(_ROOT / ".env")
_CLIENT = os.environ.get("CLIENT_NAME")
if not _CLIENT:
    sys.exit("ERROR: CLIENT_NAME is not set in fieldkit/.env")
load_dotenv(_ROOT / "clients" / _CLIENT / "src" / "photo-agent" / ".env", override=True)
```

- [x] T016 [US1] Copy `scripts/process_photos.py` → `platform/photo-agent/scripts/process_photos.py`; apply 2-step env loading above
- [x] T017 [US1] Copy `scripts/check_approval.py` → `platform/photo-agent/scripts/check_approval.py`; apply 2-step env loading
- [x] T018 [US1] Copy `scripts/upload_facebook.py` → `platform/photo-agent/scripts/upload_facebook.py`; apply 2-step env loading
- [x] T019 [P] [US1] Copy `scripts/generate_auth_link.py` → `platform/photo-agent/scripts/generate_auth_link.py`; apply 2-step env loading
- [x] T020 [P] [US1] Copy `scripts/setup_drive_auth.py` → `platform/photo-agent/scripts/setup_drive_auth.py`; apply 2-step env loading (no-op — script does not use .env)
- [x] T021 [P] [US1] Copy `scripts/run_e2e_test.py` → `platform/photo-agent/scripts/run_e2e_test.py`; update any hardcoded `clients/_demo` paths to use `platform/photo-agent`
- [x] T022 [P] [US1] Copy `scripts/e2e_stage*.py` (all 5 stage scripts) → `platform/photo-agent/scripts/`; apply 2-step env loading; update any hardcoded `clients/_demo` paths
- [x] T023 [P] [US1] Copy `scripts/__init__.py` → `platform/photo-agent/scripts/__init__.py` (verbatim)

### Tests + docs migration

- [x] T024 [P] [US1] Copy all files from `clients/_demo/src/photo-agent/tests/` → `platform/photo-agent/tests/` (verbatim — `sys.path.insert(0, parents[1])` from `tests/` now correctly points to `platform/photo-agent/`)
- [x] T025 [P] [US1] Copy `clients/_demo/src/photo-agent/requirements.txt` → `platform/photo-agent/requirements.txt` (verbatim)
- [x] T026 [P] [US1] Copy `clients/_demo/src/photo-agent/docs/facebook/` → `platform/photo-agent/docs/facebook/` (verbatim)

### Client config update

- [x] T027 [US1] Update `clients/_demo/src/photo-agent/.env` — add `FIELDKIT_DATA_DIR=<abs-path>/clients/_demo/data` and `FIELDKIT_LOG_DIR=<abs-path>/clients/_demo/logs`
- [x] T028 [US1] Update `clients/_demo/src/photo-agent/.env.example` — add `FIELDKIT_DATA_DIR` and `FIELDKIT_LOG_DIR` entries with descriptions

### Data migration (one-time operator step)

- [x] T029 [US1] Move `data/photo-agent/` from repo root → `clients/_demo/data/photo-agent/` using `mv data/photo-agent clients/_demo/data/`; verify `clients/_demo/data/photo-agent/state.json` is accessible

### Clean up _demo source

- [x] T030 [US1] Delete `clients/_demo/src/photo-agent/scripts/`, `tools/`, `tests/`, `docs/`, `requirements.txt` — leaves only `.env` and `.env.example` in `clients/_demo/src/photo-agent/`

### Verify

- [x] T031 [US1] Run `pytest platform/photo-agent/tests/ -v` from repo root — confirm all 368 tests pass (362 original + 6 new env-loading tests)

**Checkpoint**: _demo fully operational from platform code. All tests green.

---

## Phase 4: User Story 2 — New client activates with config only (Priority: P2)

**Goal**: Any future client can activate the photo-agent pipeline by creating one `.env` file — no platform code changes needed. Validated end-to-end with a permanent second client (`_construction_co`) using fully separate Drive, Telegram, and credentials from `_demo`.

**Independent Test**: Run process_photos for `_construction_co` and `_demo` back-to-back with separate `CLIENT_NAME` values — each produces its own video, sends to its own Telegram bot, and writes state to its own data directory with no cross-contamination.

### Gherkin acceptance tests

- [x] T032 [P] [US2] Write `platform/.specify/002-photo-agent/features/client-config.feature` — scenarios: missing CLIENT_NAME errors; missing FIELDKIT_DATA_DIR errors; missing FIELDKIT_LOG_DIR errors; second client activates without code changes

### Error handling

- [x] T033 [P] [US2] Create `platform/photo-agent/.env.example` documenting all required client variables: `FIELDKIT_DATA_DIR`, `FIELDKIT_LOG_DIR`, `DRIVE_ROOT_FOLDER_ID`, `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_CHAT_ID`, `FB_PAGE_ID`, `FB_PAGE_ACCESS_TOKEN`, `FB_APP_ID`, `FB_APP_SECRET`, `SECONDS_PER_PHOTO`, `VIDEO_TMP_DIR`, `GOOGLE_USER_CREDENTIALS_FILE`
- [x] T034 [US2] Add test in `platform/photo-agent/tests/test_env_loading.py` — assert missing `CLIENT_NAME` causes script to exit with error message containing "CLIENT_NAME"
- [x] T035 [US2] Add test in `platform/photo-agent/tests/test_env_loading.py` — assert missing `FIELDKIT_DATA_DIR` raises `RuntimeError` containing "FIELDKIT_DATA_DIR"
- [x] T036 [US2] Add test in `platform/photo-agent/tests/test_env_loading.py` — assert missing `FIELDKIT_LOG_DIR` raises `RuntimeError` containing "FIELDKIT_LOG_DIR"

### Second client — _construction_co

- [x] T037 [US2] Create `clients/_construction_co/` directory structure: `src/photo-agent/`, `data/photo-agent/`, `logs/`
- [x] T038 [US2] Create `clients/_construction_co/src/photo-agent/.env` with placeholder credentials; `FIELDKIT_DATA_DIR` and `FIELDKIT_LOG_DIR` set to absolute paths; no FB_* vars (pipeline scoped to Telegram approval only)
- [x] T039 [P] [US2] Create `clients/_construction_co/src/photo-agent/.env.example` — tailored for construction company with no Facebook section

### Two-client live validation (process_photos → Telegram approval only; no Facebook)

- [ ] T040 [US2] Set `CLIENT_NAME=_construction_co` in `fieldkit/.env`; upload test photos to `_construction_co` Drive root folder under a subfolder named `site_visit_01`; run `python3 platform/photo-agent/scripts/process_photos.py --project site_visit_01`; verify: video generated, Telegram approval message received on `_construction_co` bot, `clients/_construction_co/data/photo-agent/state.json` contains `site_visit_01` pending approval
- [ ] T041 [US2] Set `CLIENT_NAME=_demo` in `fieldkit/.env`; run `python3 platform/photo-agent/scripts/process_photos.py --project kitchen_remodel`; verify: video generated, Telegram approval message received on `_demo` bot (not `_construction_co`), `clients/_demo/data/photo-agent/state.json` contains `kitchen_remodel`
- [ ] T042 [US2] Verify isolation: confirm `clients/_construction_co/data/photo-agent/state.json` contains only `_construction_co` project data and `clients/_demo/data/photo-agent/state.json` contains only `_demo` project data — no cross-contamination between clients

**Checkpoint**: Two independent clients proven to operate in full isolation.

---

## Phase 5: User Story 3 — SKILL files are generic (Priority: P3)

**Goal**: All SKILL files live in `platform/photo-agent/` with no client-specific paths; any client invokes them as-is.

**Independent Test**: `grep -r "clients/_demo" platform/photo-agent/SKILL_*.md` returns no matches.

### Gherkin acceptance tests

- [x] T043 [P] [US3] Write `platform/.specify/002-photo-agent/features/skill-files.feature` — scenarios: SKILL files contain no client-specific paths; SKILL invocation loads correct client config via CLIENT_NAME

### Implementation

- [x] T044 [P] [US3] Move `clients/_demo/src/photo-agent/SKILL_process_photos.md` → `platform/photo-agent/SKILL_process_photos.md`; update bash block with `platform/photo-agent` paths
- [x] T045 [P] [US3] Move `clients/_demo/src/photo-agent/SKILL_check_approval.md` → `platform/photo-agent/SKILL_check_approval.md`; update invocation path to `platform/photo-agent/scripts/check_approval.py`
- [x] T046 [P] [US3] Move `clients/_demo/src/photo-agent/SKILL_upload_facebook.md` → `platform/photo-agent/SKILL_upload_facebook.md`; update invocation path and internal path references
- [x] T047 [P] [US3] Move `clients/_demo/src/photo-agent/SKILL_generate_auth_link.md` → `platform/photo-agent/SKILL_generate_auth_link.md`; update invocation path
- [x] T048 [US3] Verify: `grep -r "clients/_demo" platform/photo-agent/SKILL_*.md` returns no matches — confirmed clean

**Checkpoint**: All SKILL files are client-agnostic.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [ ] T049 [P] Update `CLAUDE.md` active feature section: mark Platform 002 complete; note `_construction_co` is the permanent second client; note _demo 005 (Instagram) is next
- [ ] T050 Run adversarial review of all changed files in `platform/photo-agent/` and `clients/_construction_co/` — fix any Critical→Low findings before closing the feature
- [ ] T040 [US2] Live run with `_construction_co` CLIENT_NAME → upload test photos → video → Telegram approval (requires real credentials in .env)
- [ ] T041 [US2] Live run with `_demo` CLIENT_NAME → verify isolation
- [ ] T042 [US2] Verify state file isolation between clients — no cross-contamination

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1
- **Phase 3 (US1)**: Depends on Phase 2 — BLOCKS all other user stories
- **Phase 4 (US2)**: Depends on Phase 3 (2-step loading must be in place to test error paths; _construction_co tests need real platform scripts)
- **Phase 5 (US3)**: Depends on Phase 3 (SKILL files reference platform scripts)
- **Phase 6 (Polish)**: Depends on Phases 3–5

### Within Phase 3 (US1)

- T006 (Gherkin) — parallel with tool migration
- T007–T010, T015 (verbatim tool copies) — all parallel
- T011–T014 (tool updates with fallback removal) — T011 and T012 can be parallel; T013 mirrors T011 pattern; T014 mirrors T012 pattern
- T016–T018 (core pipeline scripts) — sequential preferred; all apply same 2-step pattern
- T019–T023 (remaining scripts + __init__) — all parallel
- T024–T026 (tests, requirements, docs) — all parallel
- T027–T028 (client .env updates) — parallel
- T029 (data migration) — after T027 so FIELDKIT_DATA_DIR is set before verifying
- T030 (clean up _demo) — after T024 and T023 confirm platform versions exist
- T031 (verify 363 tests) — last in US1

### Within Phase 4 (US2)

- T032 (Gherkin), T033 (.env.example) — parallel with T034–T036
- T034–T036 (error handling tests) — parallel with each other
- T037 (_construction_co dirs) — prerequisite for T038–T040
- T038, T039 (_construction_co .env files) — parallel
- T040 (_construction_co live run) — after T038
- T041 (_demo live run) — after T031 (US1 complete); can run parallel with T040
- T042 (isolation check) — after T040 and T041

### Parallel Opportunities

**Phase 3 fast path**: Start T006–T010, T015, T019–T026 all together. Then T011–T014 (tool updates). Then T016–T018 (pipeline scripts). Then T027–T028. Then T029 → T030 → T031.

**Phase 4 fast path**: Start T032–T036 together. Then T037 → T038+T039 → T040+T041 → T042.

**Phases 5 and 6**: T043–T047 all parallel; T048 after; T049+T050 parallel.

---

## Implementation Strategy

### MVP (User Story 1 only)

1. Complete Phase 1 + Phase 2 (Setup + Root Config)
2. Complete Phase 3 (US1) — migrate everything, wire _demo, all tests green
3. **STOP and VALIDATE** — run pytest, invoke /process_photos manually
4. US1 is the complete, shippable migration

### Incremental

- Add Phase 4 (US2) — error handling + _construction_co second client — after US1 passes
- Add Phase 5 (US3 — SKILL files) after US2 passes
- Polish last

---

## Notes

- **No functional changes** — this is a pure relocation. If any test behavior changes, investigate before continuing.
- **parents[3] offset** — platform scripts are always exactly 3 levels below repo root (`platform/photo-agent/scripts/`). If this layout changes, `FIELDKIT_ROOT` env var takes precedence.
- **T029 is irreversible** — back up `data/photo-agent/` before moving, or confirm `clients/_demo/data/` is writable first.
- **T030 order matters** — only delete from `_demo/src/photo-agent/` after T031 (tests) passes; keep old files as fallback until verified.
- **_construction_co credentials** — T038 requires setting up a new Telegram bot, a new Google Drive folder, and exporting a separate `user_credentials.json` for the construction company Gmail account before T040 can run.
- **T040–T041 scope** — pipeline runs up to and including Telegram approval message only; Facebook upload is out of scope for _construction_co at this stage.
