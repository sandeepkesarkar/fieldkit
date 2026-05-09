Feature: Sender Validation
  As an admin
  I want only emails from trusted addresses to be processed
  So that the agent is not confused by spam or misconfigured senders

  Background:
    Given the email agent is running
    And Telegram is available

  Scenario: Email from unknown sender triggers rejection notification to admin
    Given my email address is NOT in the admin allowlist
    When I send an email to the agent with subject "Hello"
    And the polling cycle runs
    Then I receive a Telegram rejection notification containing:
      | field  | value                               |
      | status | ✗ Email rejected — not in allowlist |
      | From   | my email address                    |
      | Subject | Hello                              |
    And the unknown sender does NOT receive any reply
    And the email is marked as read in Gmail
    And the email does NOT have the label "fk-received"

  Scenario: Rejected email is marked as read to prevent recurrence
    Given an email arrives from an unknown sender
    When the polling cycle runs
    Then the email is marked as read in Gmail
    And the email does NOT appear in the next polling cycle's unread results

  Scenario: Rejection is logged locally
    Given an email arrives from an unknown sender
    When the polling cycle runs
    Then a REJECTED entry is appended to the local log
    And the log entry contains the sender address and subject

  Scenario: Allowlisted sender address is matched case-insensitively
    Given the allowlist contains "Admin@Example.com"
    When an email arrives from "admin@example.com"
    And the polling cycle runs
    Then the email is processed as a valid email

  Scenario: Allowlisted sender with display name in From header is accepted
    Given the allowlist contains "admin@example.com"
    When an email arrives with From header "Jane Smith <admin@example.com>"
    And the polling cycle runs
    Then the email is processed as a valid email

  Scenario: Allowlist entries with surrounding whitespace are handled correctly
    Given the allowlist is configured as " admin@example.com , other@example.com "
    When an email arrives from "admin@example.com"
    And the polling cycle runs
    Then the email is processed as a valid email

  Scenario: Malformed From header is treated as unknown sender
    Given an email arrives with a From header that cannot be parsed to a valid email address
    When the polling cycle runs
    Then the email is rejected
    And I receive a Telegram rejection notification
    And a REJECTED entry is appended to the local log
