Feature: New client activates photo-agent with config only
  As a FieldKit developer onboarding a new client
  I want to enable the full photo-agent pipeline by providing a single .env file
  So that no platform code changes are needed per client

  Scenario: Missing CLIENT_NAME produces a clear error
    Given fieldkit/.env does not contain CLIENT_NAME
    When any platform photo-agent script is invoked
    Then the script exits within 1 second
    And the error message contains "CLIENT_NAME"
    And the error message does not reference internal file paths

  Scenario: Missing FIELDKIT_DATA_DIR produces a clear error
    Given CLIENT_NAME is set correctly in fieldkit/.env
    But the active client .env does not set FIELDKIT_DATA_DIR
    When any script that reads or writes state is invoked
    Then a RuntimeError is raised containing "FIELDKIT_DATA_DIR"

  Scenario: Missing FIELDKIT_LOG_DIR produces a clear error
    Given CLIENT_NAME is set correctly in fieldkit/.env
    But the active client .env does not set FIELDKIT_LOG_DIR
    When any script that writes to the activity log is invoked
    Then a RuntimeError is raised containing "FIELDKIT_LOG_DIR"

  Scenario: _construction_co runs process_photos independently from _demo
    Given clients/_construction_co/src/photo-agent/.env has its own Drive, Telegram, and Gmail credentials
    And fieldkit/.env sets CLIENT_NAME=_construction_co
    And photos exist in _construction_co's Drive root folder under subfolder site_visit_01
    When process_photos.py is run with --project site_visit_01
    Then a video is generated from _construction_co's photos
    And a Telegram approval message is sent to _construction_co's bot
    And state is written to clients/_construction_co/data/photo-agent/state.json

  Scenario: _demo is unaffected when _construction_co runs
    Given both _demo and _construction_co are configured with separate credentials
    When CLIENT_NAME=_construction_co runs process_photos for site_visit_01
    And CLIENT_NAME=_demo runs process_photos for kitchen_remodel
    Then clients/_demo/data/photo-agent/state.json contains only kitchen_remodel
    And clients/_construction_co/data/photo-agent/state.json contains only site_visit_01
    And neither state file references the other client's projects
