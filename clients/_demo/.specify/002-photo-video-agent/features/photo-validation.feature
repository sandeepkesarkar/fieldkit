Feature: Photo Validation
  As an admin
  I want clear error messages when the Drive folder is missing or misconfigured
  So that I can correct the problem and re-trigger without confusion

  Background:
    Given the photo video agent is running
    And the Drive root folder is configured
    And Telegram is available

  Scenario: No project name provided — usage hint returned
    When I send "/process_photos" via Telegram with no project name
    Then I receive a Telegram message containing:
      | content                              |
      | "Please provide a project name"      |
      | "/process_photos kitchen_remodel"    |
    And no Drive lookup is attempted
    And no video is generated

  Scenario: Drive folder matching project name does not exist
    Given no Drive folder named "nonexistent_project" exists under the root
    When I send "/process_photos nonexistent_project" via Telegram
    Then I receive a Telegram error message containing:
      | content                              |
      | "No Drive folder found"              |
      | "nonexistent_project"                |
    And no photos are downloaded
    And no video is generated

  Scenario: Drive folder exists but contains no supported image files
    Given the "empty_project" Drive folder contains only a PDF and a HEIC file
    When I send "/process_photos empty_project" via Telegram
    Then I receive a Telegram error message containing:
      | content                              |
      | "At least 2 photos are required"     |
      | "empty_project"                      |
      | "0 image(s)"                         |
    And no video is generated

  Scenario: Drive folder contains exactly one supported image
    Given the "solo_project" Drive folder contains exactly 1 JPEG file
    When I send "/process_photos solo_project" via Telegram
    Then I receive a Telegram error message containing:
      | content                              |
      | "At least 2 photos are required"     |
      | "1 image(s)"                         |
    And no video is generated

  Scenario: Drive folder contains more than 30 supported images
    Given the "large_project" Drive folder contains 31 JPEG files
    When I send "/process_photos large_project" via Telegram
    Then I receive a Telegram error message containing:
      | content                              |
      | "Too many photos (max 30)"           |
    And no video is generated

  Scenario: Unsupported file types are silently skipped
    Given the "mixed_project" Drive folder contains:
      | filename        | type |
      | 01_photo.jpg    | JPEG |
      | 02_photo.png    | PNG  |
      | 03_doc.pdf      | PDF  |
      | 04_raw.heic     | HEIC |
    When I send "/process_photos mixed_project" via Telegram
    Then the agent uses only "01_photo.jpg" and "02_photo.png"
    And no error is reported for the PDF or HEIC files
    And the video is generated from 2 photos

  Scenario: Zero-byte image files are silently skipped
    Given the "sparse_project" Drive folder contains:
      | filename      | size    |
      | 01_photo.jpg  | 2.3 MB  |
      | 02_empty.jpg  | 0 bytes |
      | 03_photo.jpg  | 1.8 MB  |
    When I send "/process_photos sparse_project" via Telegram
    Then the zero-byte file "02_empty.jpg" is skipped
    And the video is generated from "01_photo.jpg" and "03_photo.jpg"

  Scenario: A photo download failure aborts processing with a clear error
    Given the "broken_project" Drive folder contains 4 photos
    And the second photo cannot be downloaded (Drive API error)
    When I send "/process_photos broken_project" via Telegram
    Then I receive a Telegram error message containing "Failed to download photo"
    And the temp directory is cleaned up
    And no video is generated
    And no Drive upload is attempted

  Scenario: FFmpeg not installed produces a clear setup error
    Given FFmpeg is not installed on the Mac Mini
    When I send "/process_photos kitchen_remodel" via Telegram
    Then I receive a Telegram error message containing:
      | content                           |
      | "FFmpeg not found"                |
      | "check Mac Mini setup"            |
    And no video file is created
