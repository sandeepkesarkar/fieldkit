Feature: Local Logging
  As an admin or developer
  I want all significant events logged locally
  So that I can diagnose issues and audit what the agent did

  Background:
    Given the photo video agent is running
    And "logs/photo-agent.log" is the local log file

  Scenario: Command received is logged
    When I send "/process_photos kitchen_remodel" via Telegram
    Then a COMMAND line is appended to the log containing:
      | field   | value           |
      | event   | COMMAND         |
      | project | kitchen_remodel |

  Scenario: Photo download completion is logged with count
    Given the "kitchen_remodel" Drive folder contains 6 photos
    When the agent downloads the photos
    Then a DOWNLOADED line is appended to the log containing:
      | field   | value           |
      | event   | DOWNLOADED      |
      | project | kitchen_remodel |
      | count   | 6               |

  Scenario: Video generation completion is logged with duration and file size
    When the agent generates a video
    Then a GENERATED line is appended to the log containing:
      | field        | value            |
      | event        | GENERATED        |
      | project      | kitchen_remodel  |
      | duration_sec | a positive number|
      | size_bytes   | a positive number|

  Scenario: Drive upload completion is logged with the Drive file ID
    When the agent uploads the video to Drive
    Then an UPLOADED line is appended to the log containing:
      | field         | value           |
      | event         | UPLOADED        |
      | project       | kitchen_remodel |
      | drive_file_id | a non-empty ID  |

  Scenario: Approval request sent is logged with Telegram message ID
    When the agent sends the approval request to the admin
    Then an APPROVAL_REQ line is appended to the log containing:
      | field      | value                  |
      | event      | APPROVAL_REQ           |
      | project    | kitchen_remodel        |
      | message_id | a positive integer     |

  Scenario: Approval is logged when admin approves
    When the admin approves the video
    And check_approval.py processes the approval
    Then an APPROVED line is appended to the log containing:
      | field   | value           |
      | event   | APPROVED        |
      | project | kitchen_remodel |

  Scenario: Rejection is logged when admin rejects
    When the admin rejects the video
    And check_approval.py processes the rejection
    Then a REJECTED line is appended to the log containing:
      | field   | value           |
      | event   | REJECTED        |
      | project | kitchen_remodel |

  Scenario: Phase errors are logged with phase name and detail
    Given video generation fails with an FFmpeg error
    When the agent attempts to generate the video
    Then an ERROR line is appended to the log containing:
      | field   | value              |
      | event   | ERROR              |
      | project | kitchen_remodel    |
      | phase   | generate           |
      | detail  | the FFmpeg message |

  Scenario: Log lines are appended, not overwritten
    Given the log file already contains 10 lines from previous runs
    When the agent completes a full run
    Then the log file contains more than 10 lines
    And the original 10 lines are still present at the top of the file

  Scenario: Log directory is created if it does not exist
    Given the "logs/" directory does not exist
    When any log function is called for the first time
    Then the "logs/" directory is created
    And the log line is written successfully

  Scenario: Timestamps in log lines use the format YYYY-MM-DD HH:MM
    When any event is logged
    Then the timestamp at the start of the log line matches the pattern "YYYY-MM-DD HH:MM"

  Scenario: Sensitive data does not appear in log output
    When the agent runs a full cycle including an approval
    Then the log file does not contain the admin email address
    And the log file does not contain the Telegram chat ID
    And the log file does not contain the Telegram bot token
