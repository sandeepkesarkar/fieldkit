Feature: Approved Video Auto-Posts to Instagram
  As a small business owner
  I want approved videos to be posted automatically to my Instagram account
  So that I don't have to manually upload content after approving it in Telegram, on top of the automatic Facebook post

  Background:
    Given a Facebook Page is linked to FieldKit with a valid access token
    And an Instagram Business account is linked to that Page with ID "17841400000000000"
    And a video has been generated and is waiting for approval in Telegram

  Scenario: Approved video is posted to Instagram within 5 minutes
    Given a video has been approved via Telegram
    When the approval is processed by check_approval.py
    Then an InstagramUploadJob is enqueued in "pending" status with the video's idempotency key
    And when upload_instagram.py runs on its next cron tick
    Then a media container is created via the Instagram Graph API
    And the job status transitions to "uploading"
    And once the container status is "FINISHED" the container is published
    And the job status transitions to "published"
    And the owner receives a Telegram message containing "Reel live on Instagram"
    And the message includes a direct link to the Instagram post

  Scenario: A single Telegram approval triggers both platforms
    Given a video has been approved via Telegram
    When the approval is processed by check_approval.py
    Then both a VideoUploadJob (Facebook) and an InstagramUploadJob are enqueued
    And both jobs share the same idempotency key
    And the owner is not asked to approve the video a second time

  Scenario: No caption is applied when no caption template is configured
    Given no caption template is configured for the client
    When a video is approved and published to Instagram
    Then the Instagram media container is created with no caption text

  Scenario: The temporary Drive share link is revoked after a successful publish
    Given a video has been approved and its InstagramUploadJob is in progress
    When the Instagram media container reaches "FINISHED" and is published
    Then the temporary Google Drive share link created for that video is revoked
    And the video is no longer reachable via that share link

  Scenario: Instagram publishing is skipped entirely when not configured for a client
    Given the client's .env has no IG_BUSINESS_ACCOUNT_ID set
    When a video is approved via Telegram
    Then no InstagramUploadJob is enqueued
    And upload_instagram.py exits with code 0 on its next cron tick without any Instagram API calls
    And the existing Facebook upload flow (Feature 003) proceeds unaffected
