Feature: Approval Flow
  As an admin
  I want to approve or reject a generated video via Telegram
  So that only videos I have reviewed are delivered

  Background:
    Given the photo video agent is running
    And a video has been generated for project "kitchen_remodel"
    And an approval request has been sent via Telegram
    And "state.json" contains a pending_approval record for "kitchen_remodel"
    And Telegram is available

  Scenario: Admin approves the video — email delivered and state cleared
    When I tap ✅ Approve in Telegram
    And the approval check runs
    Then I receive an approval email at my configured admin email address
    And the email subject contains "kitchen_remodel"
    And the email body contains the Drive folder link
    And I receive a Telegram confirmation message containing "✅ Approved"
    And the local temp video file is deleted
    And "state.json" pending_approval is null

  Scenario: Admin rejects the video — video removed and state cleared
    When I tap ❌ Reject in Telegram
    And the approval check runs
    Then the generated video is removed from the "kitchen_remodel" Drive folder
    And the local temp video file is deleted
    And I receive a Telegram notification containing "❌ Rejected"
    And the Telegram message instructs me to update the photos and re-trigger
    And "state.json" pending_approval is null

  Scenario: No approval response received — agent waits indefinitely
    Given no Telegram callback has been received
    When the approval check runs multiple times
    Then the pending_approval record remains in "state.json"
    And no email is sent
    And no Drive file is deleted
    And no further Telegram messages are sent

  Scenario: Email delivery fails on approval — Telegram fallback with Drive link
    Given the approval email cannot be delivered (gws error)
    When I tap ✅ Approve in Telegram
    And the approval check runs
    Then I receive a Telegram message containing the Drive folder link
    And the Telegram message indicates the email delivery failed
    And the local temp video file is still deleted
    And "state.json" pending_approval is null

  Scenario: Drive delete fails on rejection — rejection still completes
    Given the Drive delete call returns an error
    When I tap ❌ Reject in Telegram
    And the approval check runs
    Then I still receive the Telegram rejection notification
    And the local temp video file is deleted
    And "state.json" pending_approval is null
    And the Drive delete failure is recorded in the local log

  Scenario: Approval email is sent from the configured agent Gmail address
    When I tap ✅ Approve in Telegram
    And the approval check runs
    Then the approval email is sent via gws gmail
    And the sender is the configured AGENT_EMAIL address
    And the recipient is the configured ADMIN_EMAIL address

  Scenario: answerCallbackQuery is called before any other action
    When I tap ✅ Approve in Telegram
    And the approval check runs
    Then the Telegram callback spinner is dismissed before the email is sent
    And the callback spinner is dismissed before any Telegram message is sent

  Scenario: Rejected project can be re-triggered after admin curates Drive folder
    Given I have rejected a video for "kitchen_remodel"
    And I have updated the photos in the Drive folder
    When I send "/process_photos kitchen_remodel" via Telegram
    Then the agent processes the updated photos
    And a new approval request is sent
