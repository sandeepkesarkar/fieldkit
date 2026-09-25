Feature: Instagram Upload Failure Recovery
  As a small business owner
  I want FieldKit to recover from Instagram upload failures automatically
  So that a temporary outage doesn't require my manual intervention, and doesn't affect my Facebook post

  Background:
    Given an InstagramUploadJob is in "pending" status
    And the Facebook Page access token used for Instagram publishing is valid

  Scenario: Transient error triggers retry after 60-second cooldown
    Given upload_instagram.py encounters an API error creating the media container on the first attempt
    When the job's attempt_count is incremented to 1
    And upload_instagram.py runs again within 60 seconds
    Then the retry is skipped (cooldown not elapsed)
    And when upload_instagram.py runs after 60 seconds have passed
    Then a second container-creation attempt is made

  Scenario: Successful retry delivers normal confirmation with no failure alert
    Given the first upload attempt failed
    When a retry attempt succeeds through container creation, polling, and publish
    Then the job status transitions to "published"
    And the owner receives the normal "✅ Reel live on Instagram!" confirmation
    And no failure alert is sent

  Scenario: All three retry attempts fail — owner is alerted
    Given all three upload attempts have failed
    When the third failure is detected
    Then the job status transitions to "failed"
    And a Telegram alert is sent containing "Instagram upload failed"
    And the alert is sent within 5 minutes of the third failure
    And the owner is instructed to check the connection and retry manually

  Scenario: Expired access token skips retries and alerts immediately
    Given the Facebook Page access token has expired
    When upload_instagram.py attempts to create a media container
    And Instagram returns OAuthException with error code 190
    Then the upload is NOT retried (retrying won't help)
    And the job status transitions to "failed" immediately
    And a Telegram alert is sent containing "Instagram token expired"
    And the alert instructs the owner to reconnect their account

  Scenario: A container stuck in processing times out and is treated as a retryable failure
    Given a media container never reaches status "FINISHED"
    When upload_instagram.py has polled the container status for 3 minutes
    Then the attempt is treated as a failure and attempt_count is incremented
    And the temporary Drive share link created for that attempt is revoked
    And the job is retried on the next cron tick like any other transient failure

  Scenario: Instagram failure does not block or roll back a successful Facebook post for the same video
    Given a video's Facebook upload succeeds
    And that same video's Instagram upload fails on all three attempts
    Then the Facebook post remains published with its normal confirmation already sent
    And the Instagram job is marked "failed" with its own independent Telegram alert
    And neither platform's outcome is affected by, or contingent on, the other

  Scenario: Facebook failure does not block or roll back a successful Instagram post for the same video
    Given a video's Instagram upload succeeds
    And that same video's Facebook upload fails on all three attempts
    Then the Instagram post remains published with its normal confirmation already sent
    And the Facebook job is marked "failed" with its own independent Telegram alert (per Feature 003)
    And neither platform's outcome is affected by, or contingent on, the other
