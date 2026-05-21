Feature: Email Deduplication
  As an admin
  I want each email to be processed exactly once under normal conditions
  So that I do not receive duplicate acknowledgements or miss any emails

  Background:
    Given the email agent is running
    And my email address is in the admin allowlist
    And Telegram is available

  Scenario: Already processed email is not picked up again in the next cycle
    Given an email has been successfully processed and labelled "fk-received"
    When the next polling cycle runs
    Then the email is not processed again
    And I do not receive a duplicate Telegram acknowledgement

  Scenario: Unprocessed emails are not affected by processed emails in the same inbox
    Given one email has already been processed and labelled "fk-received"
    And one new unread email has arrived without the "fk-received" label
    When the polling cycle runs
    Then only the new email is processed
    And I receive exactly one Telegram acknowledgement

  Scenario: No emails are lost if the agent restarts mid-cycle
    Given the agent is in the middle of processing a batch of emails when it restarts
    When the agent restarts and the polling cycle runs again
    Then all emails that had not yet been labelled "fk-received" are processed
    And no emails that had already been labelled "fk-received" are reprocessed
