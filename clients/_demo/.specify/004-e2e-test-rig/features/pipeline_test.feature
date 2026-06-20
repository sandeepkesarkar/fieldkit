Feature: End-to-End Pipeline Test
  As the FieldKit admin
  I want to run a single script that exercises the full photo-agent pipeline
  So that I can verify everything is working after changes or credential rotations

  Background:
    Given the .env file contains valid credentials for Drive, Telegram, and Facebook
    And the cron jobs for check_approval.py and upload_facebook.py are running
    And no other project is currently awaiting approval in state.json
    And FFmpeg is available on the system PATH

  # US1 — Run End-to-End Pipeline Test

  Scenario: Full pipeline passes with default duration
    Given I run "python3 scripts/run_e2e_test.py" from the repo root
    When all five pipeline stages complete
    Then the script exits with code 0
    And a clock-face video showing MM/DD/YYYY HH:MM:SS is visible on the Fieldkit Facebook page
    And the Telegram bot has sent a confirmation message

  Scenario: Custom duration generates correct number of frames
    Given I run "python3 scripts/run_e2e_test.py --duration 60"
    When Stage 1 completes
    Then the number of generated JPEG frames results in a video approximately 60 seconds long
    And each frame shows a different MM/DD/YYYY HH:MM:SS timestamp advancing by one second

  Scenario: Stage failure exits immediately with a clear error
    Given the Drive credentials are expired
    When I run "python3 scripts/run_e2e_test.py"
    Then Stage 2 reports "❌" with a Drive authentication error message
    And the script exits with a non-zero code
    And no subsequent stages are attempted

  Scenario: Pre-flight rejects missing required env var
    Given the env var FB_PAGE_ACCESS_TOKEN is not set
    When I run "python3 scripts/run_e2e_test.py"
    Then the script exits with a non-zero code before Stage 1
    And the error message names the missing variable

  Scenario: Pre-flight rejects another pending approval
    Given state.json already contains a pending approval for a different project
    When I run "python3 scripts/run_e2e_test.py"
    Then the script exits with a non-zero code before Stage 1
    And the error message mentions "pending approval"

  Scenario: Pre-flight rejects duration below minimum
    When I run "python3 scripts/run_e2e_test.py --duration 1"
    Then the script exits with a non-zero code before Stage 1
    And the error message states the minimum is 2 seconds

  Scenario: Pre-flight rejects duration above maximum
    When I run "python3 scripts/run_e2e_test.py --duration 301"
    Then the script exits with a non-zero code before Stage 1
    And the error message states the maximum is 300 seconds

  # US2 — Observe Real-Time Stage Progress

  Scenario: Each stage prints a timestamped status line
    Given I run "python3 scripts/run_e2e_test.py --duration 30"
    When the script is running
    Then each completed stage prints a line matching "[HH:MM:SS] Stage N/5: <label> ✅ done (Xs)"
    And the line appears within 5 seconds of the stage completing

  Scenario: Stage timeout prints a clear message and exits
    Given the approval stage timeout is set to 5 seconds via "--approval-timeout 5"
    When the admin does not tap Approve within 5 seconds
    Then the script prints a line matching "[HH:MM:SS] Stage 4/5: Approval received ❌ timed out after 5s"
    And the script exits with a non-zero code

  Scenario: Final summary shows total elapsed time on success
    Given all five stages complete successfully
    When the script exits
    Then the final output line shows "✅ All stages passed. Total: Xm Ys"
    And the Facebook post URL is printed on the following line
