Feature: Concurrent Processing Guard
  As the system
  I want to prevent overlapping runs and multiple simultaneous approvals
  So that state is never corrupted and the admin always has a clear next action

  Background:
    Given the photo video agent is running
    And Telegram is available

  Scenario: Second /process_photos while a run is already in progress exits silently
    Given process_photos.py is currently running and holds run.lock
    When a second /process_photos command arrives
    Then the second instance detects the lock is held
    And the second instance exits immediately without processing
    And no Telegram message is sent by the second instance
    And the first instance continues to completion unaffected

  Scenario: /process_photos while an approval is pending returns a clear error
    Given "state.json" contains a pending_approval record for "kitchen_remodel"
    When I send "/process_photos kitchen_remodel" via Telegram
    Then I receive a Telegram error message containing:
      | content                                 |
      | "already awaiting approval"             |
      | "kitchen_remodel"                       |
      | "✅ Approve or ❌ Reject"               |
    And no photos are downloaded
    And no new video is generated
    And the existing pending_approval record is unchanged

  Scenario: check_approval exits immediately when no approval is pending
    Given "state.json" has pending_approval set to null
    When check_approval.py runs
    Then it exits immediately
    And no Telegram API calls are made
    And no Drive calls are made

  Scenario: Telegram update offset is incremented after each check_approval run
    Given the current Telegram update offset is 100
    And the Telegram API returns updates with IDs 101, 102, 103
    When check_approval.py runs
    Then the new offset stored in "state.json" is 104
    And subsequent runs use offset 104 when calling getUpdates

  Scenario: The same callback_query is not processed twice
    Given check_approval.py ran and stored offset 104 after processing an Approve callback
    When check_approval.py runs again
    Then it calls getUpdates with offset 104
    And the earlier Approve callback (update_id 103) is not returned
    And no duplicate approval email is sent

  Scenario: Agent restart does not lose a pending approval
    Given "state.json" has a pending_approval record written before the restart
    When the Mac Mini restarts and check_approval.py next runs via cron
    Then the pending_approval record is read from "state.json"
    And the approval check proceeds normally using the stored Telegram message ID
