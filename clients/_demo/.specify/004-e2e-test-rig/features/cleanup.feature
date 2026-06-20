Feature: Clean Up Test Artifacts
  As the FieldKit admin
  I want to remove the Drive folder and Facebook post created by a test run
  So that repeated test runs do not accumulate clutter

  Background:
    Given a previous test run completed successfully
    And the Drive test folder "e2e-test-YYYYMMDD-HHMMSS" exists in Google Drive
    And a test Facebook post exists on the Fieldkit demo page

  # US3 — Clean Up Test Artifacts

  Scenario: Cleanup removes Drive folder and Facebook post
    When I run "python3 scripts/run_e2e_test.py --cleanup"
    Then the Drive folder for the test run is deleted
    And the Facebook test post is deleted
    And the script exits with code 0

  Scenario: Cleanup warns but succeeds when Facebook post is already deleted
    Given the Facebook test post has already been deleted manually
    When I run "python3 scripts/run_e2e_test.py --cleanup"
    Then the script logs a warning that the Facebook post was not found
    And the Drive folder is still deleted
    And the script exits with code 0

  Scenario: Second test run succeeds after cleanup
    Given cleanup has been run after the first test
    When I run "python3 scripts/run_e2e_test.py --duration 30" again
    Then the script completes all five stages successfully
    And no orphaned state from the previous run interferes
