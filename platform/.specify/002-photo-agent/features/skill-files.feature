Feature: SKILL files are client-agnostic
  As a FieldKit developer adding a new client
  I want platform SKILL files to work for any client without modification
  So that onboarding a new client requires no SKILL file edits

  Scenario: SKILL files contain no client-specific paths
    Given all SKILL files reside in platform/photo-agent/
    When the SKILL files are inspected for client-specific content
    Then no SKILL file contains the string "clients/_demo"
    And no SKILL file contains hardcoded secrets or credentials

  Scenario: SKILL invocation uses correct client config via CLIENT_NAME
    Given CLIENT_NAME=_demo is set in fieldkit/.env
    And platform/photo-agent/SKILL_process_photos.md invokes the platform script
    When the operator triggers /process_photos kitchen_remodel
    Then the platform script resolves CLIENT_NAME from fieldkit/.env
    And loads secrets from clients/_demo/src/photo-agent/.env
    And runs the pipeline with _demo credentials

  Scenario: New client uses same SKILL files without modification
    Given CLIENT_NAME=_acme is set in fieldkit/.env
    And clients/_acme/src/photo-agent/.env contains valid _acme config
    When the operator triggers /process_photos my_project
    Then the same unmodified platform SKILL file is used
    And the pipeline runs with _acme credentials
