Feature: Undelivered Notification Alert (Dead-Letter Queue)
  As an admin
  I want to be alerted if Telegram notifications may not have been delivered
  So that I can confirm receipt without the system doing complex retries

  Background:
    Given the email agent is running
    And my email address is in the admin allowlist

  Scenario: Pending entry is written before Telegram send
    When a valid email is processed
    Then an entry is added to pending.json before the Telegram message is sent
    And the entry contains the ref ID, Gmail message ID, sender, subject, and a UTC timestamp

  Scenario: Pending entry is removed after successful Telegram send
    Given a valid email is processed and Telegram accepts the message
    Then the entry is removed from pending.json after the send
    And no stale alert is triggered on the next cycle

  Scenario: Stale entry triggers alert email to admin
    Given pending.json contains an entry that is more than 15 minutes old
    When a polling cycle runs
    Then I receive an alert email at my admin address
    And the email subject contains "Possible undelivered notifications"
    And the email body lists the stale ref ID and subject
    And the stale entry is removed from pending.json after the alert is sent

  Scenario: Stale alert is logged
    Given pending.json contains a stale entry
    When the stale check runs
    Then a STALE_ALERT entry is appended to the local log
    And the entry contains the count and ref IDs of the stale notifications

  Scenario: Fresh pending entries are not alerted
    Given pending.json contains an entry that is less than 15 minutes old
    When a polling cycle runs
    Then no alert email is sent
    And the entry remains in pending.json

  Scenario: Multiple stale entries produce a single alert email
    Given pending.json contains 3 entries all older than 15 minutes
    When a polling cycle runs
    Then I receive exactly one alert email listing all 3 ref IDs
    And all 3 entries are removed from pending.json
