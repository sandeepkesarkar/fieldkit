Feature: On-Demand Inbox Check
  As an admin
  I want to trigger an immediate inbox check via Telegram
  So that I do not have to wait for the next polling cycle

  Background:
    Given the email agent is running
    And my email address is in the admin allowlist
    And Telegram is available

  Scenario: /check-email with pending emails responds immediately
    Given I have sent an email to the agent that has not yet been processed
    When I send "/check-email" via Telegram
    Then I receive a Telegram acknowledgement without waiting for the polling cycle
    And the acknowledgement format matches the standard receipt format

  Scenario: /check-email with no pending emails responds with "No new emails."
    Given there are no unread emails without the "fk-received" label in the agent inbox
    When I send "/check-email" via Telegram
    Then I receive the message "No new emails."

  Scenario: /check-email with multiple pending emails responds with one ack per email
    Given I have sent 3 emails to the agent that have not yet been processed
    When I send "/check-email" via Telegram
    Then I receive 3 separate Telegram acknowledgements
    And each acknowledgement has a unique reference ID

  Scenario: Cron cycle with no new emails sends no Telegram message
    Given there are no unread emails without the "fk-received" label in the agent inbox
    When the cron polling cycle fires
    Then no Telegram message is sent
    And a CYCLE log entry is written with processed=0 rejected=0

  Scenario: /check-email response format is identical to polling cycle response
    Given I have sent an email that will be processed by /check-email
    When I send "/check-email" via Telegram
    Then the Telegram acknowledgement contains:
      | field       | value                      |
      | status      | ✓ Email received           |
      | From        | my email address           |
      | Ref         | a valid reference ID #NNNN |
