Feature: Upload Failure Recovery
  As a small business owner
  I want FieldKit to recover from upload failures automatically
  So that a temporary outage doesn't require my manual intervention

  Background:
    Given a VideoUploadJob is in "pending" status
    And the Facebook Page access token is valid

  Scenario: Transient error triggers retry after 60-second cooldown
    Given upload_facebook.py encounters an API error on the first attempt
    When the job's attempt_count is incremented to 1
    And upload_facebook.py runs again within 60 seconds
    Then the retry is skipped (cooldown not elapsed)
    And when upload_facebook.py runs after 60 seconds have passed
    Then a second upload attempt is made to Facebook

  Scenario: Successful retry delivers normal confirmation with no failure alert
    Given the first upload attempt failed
    When a retry attempt succeeds
    Then the job status transitions to "published"
    And the owner receives the normal "✅ Video live on Facebook!" confirmation
    And no failure alert is sent

  Scenario: All three retry attempts fail — owner is alerted
    Given all three upload attempts have failed
    When the third failure is detected
    Then the job status transitions to "failed"
    And a Telegram alert is sent containing "Facebook upload failed"
    And the alert is sent within 5 minutes of the first failure
    And the owner is instructed to check the connection and retry manually

  Scenario: Expired access token skips retries and alerts immediately
    Given the Facebook access token has expired
    When upload_facebook.py attempts to upload the video
    And Facebook returns OAuthException with error code 190
    Then the upload is NOT retried (retrying won't help)
    And the job status transitions to "failed" immediately
    And a Telegram alert is sent containing "Facebook token expired"
    And the alert instructs the owner to reconnect their Facebook Page

  Scenario: Retry succeeds on second attempt — no partial failure alert
    Given the first upload attempt failed with a transient error
    When the second upload attempt succeeds
    Then the job status transitions to "published"
    And the owner receives only the success confirmation
    And no failure alert is sent at any point
