Feature: _demo client works unchanged after migration
  As the sole FieldKit operator
  I want the _demo photo-agent pipeline to behave identically after code moves to platform
  So that no existing functionality is broken by the migration

  Background:
    Given the photo-agent code lives in platform/photo-agent/
    And clients/_demo/src/photo-agent/ contains only .env and .env.example
    And fieldkit/.env sets CLIENT_NAME=_demo and FIELDKIT_ROOT to the repo root

  Scenario: Full test suite passes after migration
    Given the platform/photo-agent/tests/ directory contains all migrated tests
    When pytest is run against platform/photo-agent/tests/
    Then all 363 tests pass with no failures or errors

  Scenario: process_photos pipeline runs end-to-end for _demo
    Given _demo .env sets FIELDKIT_DATA_DIR and FIELDKIT_LOG_DIR
    And DRIVE_ROOT_FOLDER_ID points to a valid Drive folder
    When the operator invokes /process_photos kitchen_remodel
    Then the platform script loads _demo credentials
    And generates a video from Drive photos
    And sends an approval message via Telegram
    And writes state to clients/_demo/data/photo-agent/state.json

  Scenario: State files are accessible at new per-client location
    Given data/photo-agent/ state files have been moved to clients/_demo/data/photo-agent/
    And FIELDKIT_DATA_DIR is set to the clients/_demo/data absolute path
    When any platform script reads or writes state
    Then it reads from and writes to clients/_demo/data/photo-agent/
    And no files are written to the old repo-root data/photo-agent/ location
