Feature: Reference ID Management
  As an admin
  I want each email to receive a unique persistent reference ID
  So that I can track and refer back to specific emails

  Background:
    Given the email agent is running
    And my email address is in the admin allowlist
    And Telegram is available

  Scenario: First email ever receives reference ID #0001
    Given no emails have ever been processed
    When I send an email to the agent
    And the polling cycle runs
    Then the Telegram acknowledgement contains "Ref: #0001"

  Scenario: Reference IDs increment sequentially
    Given 5 emails have already been processed and the last ref ID is #0005
    When I send a new email to the agent
    And the polling cycle runs
    Then the Telegram acknowledgement contains "Ref: #0006"

  Scenario: Reference IDs are zero-padded to 4 digits
    Given 9 emails have already been processed
    When I send a new email
    And the polling cycle runs
    Then the Telegram acknowledgement contains "Ref: #0010"

  Scenario: Reference ID counter persists across agent restarts
    Given the agent has processed emails up to ref ID #0010
    When the agent process restarts
    And I send a new email to the agent
    And the polling cycle runs
    Then the Telegram acknowledgement contains "Ref: #0011"
    And no previously processed emails are reprocessed

  Scenario: Crash recovery reuses the same reference ID for the same email
    Given an email was partially processed and assigned ref ID #0007
    But the agent crashed before marking the email with "fk-received"
    When the agent restarts and the polling cycle runs
    Then the email is processed again
    And the Telegram acknowledgement contains "Ref: #0007"
    And the ref ID counter does not advance for this email
