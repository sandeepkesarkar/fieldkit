Feature: Video Generation
  As an admin
  I want the generated video to be correctly formatted and ordered
  So that it is ready to post on Facebook, Instagram, and WhatsApp

  Background:
    Given the photo video agent is running
    And a Drive project folder contains photos

  Scenario: Generated video meets social media format requirements
    Given the folder contains 3 photos
    When the agent generates a video
    Then the output file is an MP4
    And the video codec is H.264 (libx264)
    And the resolution is 1080 × 1920 pixels
    And the aspect ratio is 9:16 (portrait)
    And the frame rate is 30 fps
    And the audio codec is AAC with no audio track (silent)

  Scenario: Photos appear in the video in filename-alphabetical order
    Given the folder contains photos named:
      | filename        |
      | 03_finished.jpg |
      | 01_start.jpg    |
      | 02_progress.jpg |
    When the agent generates a video
    Then the photos appear in order: "01_start.jpg", "02_progress.jpg", "03_finished.jpg"

  Scenario: Each photo is displayed for the configured duration
    Given seconds_per_photo is set to 4
    And the folder contains 5 photos
    When the agent generates a video
    Then the total video duration is 18 seconds
    And the formula applied is: (5 × 4) − (4 × 0.5) = 18 sec

  Scenario: Crossfade transitions appear between each photo
    Given the folder contains 4 photos
    When the agent generates a video
    Then there are 3 crossfade transitions in the video
    And each transition is 0.5 seconds long

  Scenario: Landscape photos are scaled and center-cropped to fill the frame
    Given the folder contains a landscape photo (wider than tall)
    When the agent generates a video
    Then the photo fills the full 1080 × 1920 frame
    And no black bars (letterboxing) appear in the video

  Scenario: A 5-photo video stays under 16 MB for WhatsApp compatibility
    Given seconds_per_photo is set to 4
    And the folder contains 5 standard photos
    When the agent generates a video
    Then the output file size is under 16 MB

  Scenario: Privacy scrub step exists in the pipeline as a no-op
    Given the pipeline runs normally
    When the agent processes photos
    Then a scrub step is called between photo download and video generation
    And the scrub step returns the photos unchanged in this phase

  Scenario: VideoGenerator implementation is replaceable without changing the pipeline
    Given the pipeline uses FFmpegVideoGenerator
    When FFmpegVideoGenerator is replaced with a different VideoGenerator implementation
    Then process_photos.py requires no changes beyond the constructor call
    And the replacement implementation produces a video at the configured output path
