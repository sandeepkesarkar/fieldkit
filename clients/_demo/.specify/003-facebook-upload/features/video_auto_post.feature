Feature: Approved Video Auto-Posts to Facebook
  As a small business owner
  I want approved videos to be posted automatically to my Facebook Page
  So that I don't have to manually upload content after approving it in Telegram

  Background:
    Given a Facebook Page is linked to FieldKit with a valid access token
    And a video has been generated and is waiting for approval in Telegram

  Scenario: Approved video is posted to Facebook within 2 minutes
    Given a video has been approved via Telegram
    When the approval is processed by check_approval.py
    Then a VideoUploadJob is enqueued with status "pending"
    And when upload_facebook.py runs within the next cron tick
    Then the video is uploaded to the linked Facebook Page
    And the job status transitions to "published"
    And the post is visible as a public post on the Facebook Page

  Scenario: Owner receives Telegram confirmation after successful post
    Given a video has been successfully uploaded to Facebook
    When the post goes live
    Then the owner receives a Telegram message containing "✅ Video live on Facebook!"
    And the message includes a direct link to the Facebook post

  Scenario: Video is posted with no caption
    Given no caption template is configured
    When a video is approved and uploaded to Facebook
    Then the Facebook post contains the video with no caption text

  Scenario: Duplicate approval is ignored
    Given a video has already been published to Facebook with idempotency key "msg_42"
    When the same video approval fires again with idempotency key "msg_42"
    Then no new VideoUploadJob is enqueued
    And no duplicate post appears on the Facebook Page

  Scenario: Missing video file is handled gracefully
    Given a video approval triggers an upload job
    But the video file is missing from disk
    When upload_facebook.py processes the job
    Then the job status transitions to "failed"
    And no upload attempt is made to Facebook
    And the owner receives a Telegram alert about the missing file
