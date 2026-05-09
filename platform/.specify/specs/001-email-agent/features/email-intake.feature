Feature: Email Intake — Happy Path
  As an admin
  I want emails sent to the agent to be acknowledged via Telegram
  So that I know my instructions have been received

  Background:
    Given the email agent is running
    And my email address is in the admin allowlist
    And Telegram is available

  Scenario: Single email is acknowledged within one polling cycle
    When I send an email to the agent with subject "Before/after photos — Job #42" and 3 attachments
    And the polling cycle runs
    Then I receive a Telegram acknowledgement containing:
      | field       | value                         |
      | status      | ✓ Email received              |
      | From        | my email address              |
      | Subject     | Before/after photos — Job #42 |
      | Attachments | 3                             |
      | Ref         | a valid reference ID #NNNN    |
    And the email is marked as read in Gmail
    And the email has the label "fk-received" in Gmail

  Scenario: Multiple emails in one cycle are each acknowledged individually
    When I send 3 emails to the agent
    And the polling cycle runs
    Then I receive 3 separate Telegram acknowledgements
    And each acknowledgement has a unique reference ID
    And all 3 emails are marked as read and labelled "fk-received"

  Scenario: Email with no attachments is acknowledged correctly
    When I send an email with no attachments
    And the polling cycle runs
    Then I receive a Telegram acknowledgement with "Attachments: 0"

  Scenario: A single failing email does not block the rest of the cycle
    Given one of my emails will cause a gws API error during processing
    When I send 3 emails to the agent including the failing one
    And the polling cycle runs
    Then I receive Telegram acknowledgements for the 2 emails that processed successfully
    And a CYCLE log entry records processed=2
