Feature: Local Logging
  As a system operator
  I want all agent events logged locally with timestamps
  So that I can audit what happened and diagnose issues

  Background:
    Given the email agent is running

  Scenario: Valid email receipt is logged
    Given a valid email is processed and the Telegram ack is sent
    Then a RECEIVED entry is appended to the local log containing:
      | field       | value              |
      | timestamp   | YYYY-MM-DD HH:MM   |
      | event       | RECEIVED           |
      | from        | sender address     |
      | subject     | email subject      |
      | attachments | attachment count   |
      | ref         | reference ID #NNNN |

  Scenario: Invalid sender rejection is logged
    Given an email arrives from an unknown sender
    When the polling cycle runs
    Then a REJECTED entry is appended to the local log containing:
      | field     | value            |
      | timestamp | YYYY-MM-DD HH:MM |
      | event     | REJECTED         |
      | from      | sender address   |
      | subject   | email subject    |

  Scenario: Stale notification alert is logged
    Given pending.json contains a stale entry when the cycle runs
    Then a STALE_ALERT entry is appended to the local log containing:
      | field     | value                        |
      | timestamp | YYYY-MM-DD HH:MM             |
      | event     | STALE_ALERT                  |
      | count     | number of stale entries      |
      | refs      | comma-separated ref ID list  |

  Scenario: Polling cycle completion is logged with counts
    Given a polling cycle processes 2 valid emails and rejects 1
    Then a CYCLE entry is appended to the local log containing:
      | field     | value            |
      | timestamp | YYYY-MM-DD HH:MM |
      | event     | CYCLE            |
      | processed | 2                |
      | rejected  | 1                |

  Scenario: Log entries are appended and never overwritten
    Given the log file already contains previous entries
    When a new event occurs
    Then the new entry appears at the end of the log file
    And all previous log entries remain unchanged

  Scenario: Log directory is created automatically if it does not exist
    Given the log directory ~/src/fieldkit/logs does not exist
    When the first log entry is written
    Then the directory is created
    And the log entry is written successfully
