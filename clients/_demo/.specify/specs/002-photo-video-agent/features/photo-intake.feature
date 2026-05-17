Feature: Photo Intake — Happy Path
  As an admin
  I want to trigger video generation from a Google Drive project folder
  So that I receive a video ready for social media approval

  Background:
    Given the photo video agent is running
    And the Drive root folder is configured
    And a Drive project folder named "kitchen_remodel" exists under the root
    And the folder contains 5 photos named "01_hero.jpg", "02_progress.jpg", "03_done.jpg", "04_detail.jpg", "05_final.jpg"
    And Telegram is available

  Scenario: Admin triggers video generation and receives approval request
    When I send "/process_photos kitchen_remodel" via Telegram
    Then the agent downloads all 5 photos from the Drive folder
    And the agent generates a video from the photos
    And the agent uploads the video to the "kitchen_remodel" Drive folder
    And I receive a Telegram message containing:
      | field        | value                         |
      | project      | kitchen_remodel               |
      | photos       | 5                             |
      | duration     | 18 sec                        |
      | Drive link   | a link to the Drive folder    |
      | buttons      | ✅ Approve and ❌ Reject       |

  Scenario: Generated video appears in the same Drive folder as the source photos
    When I send "/process_photos kitchen_remodel" via Telegram
    Then the Drive folder "kitchen_remodel" contains the original photos
    And the Drive folder "kitchen_remodel" also contains a new .mp4 file
    And the .mp4 filename includes the project name and a UTC timestamp

  Scenario: Pending approval state is written after video is sent for review
    When I send "/process_photos kitchen_remodel" via Telegram
    And the approval request has been sent
    Then "state.json" contains a pending_approval record with:
      | field              | value                   |
      | project_name       | kitchen_remodel         |
      | drive_folder_id    | the Drive folder ID     |
      | drive_video_file_id| the uploaded video ID   |
      | telegram_message_id| the approval message ID |
      | triggered_at       | a UTC ISO 8601 timestamp|

  Scenario: Temp directory is cleared before each new run for the same project
    Given a previous run left files in "data/photo-agent/tmp/kitchen_remodel/"
    When I send "/process_photos kitchen_remodel" via Telegram
    Then the temp directory is cleared before photos are downloaded
    And only the current run's photos are present during processing
