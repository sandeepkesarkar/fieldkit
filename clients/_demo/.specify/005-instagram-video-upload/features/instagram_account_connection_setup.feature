Feature: Instagram Account Connection Setup
  As an administrator
  I want to discover the Instagram account linked to a client's Facebook Page
  So that FieldKit can publish Reels on that account without a separate OAuth flow

  Scenario: Connection check discovers a linked Business account
    Given the client's Facebook Page (connected under Feature 003) has a linked Instagram account "@my_business_demo"
    And that Instagram account's type is "BUSINESS"
    When the administrator runs check_instagram_connection.py
    Then FieldKit reports "Found linked Instagram account: @my_business_demo (ID: 17841400000000000)"
    And IG_BUSINESS_ACCOUNT_ID is written to the client's .env file
    And existing .env variables are preserved
    And the script exits with code 0

  Scenario: Connection check discovers a linked Creator account
    Given the client's Facebook Page has a linked Instagram account "@my_creator_demo"
    And that Instagram account's type is "CREATOR"
    When the administrator runs check_instagram_connection.py
    Then IG_BUSINESS_ACCOUNT_ID is written to the client's .env file
    And the script exits with code 0

  Scenario: No Instagram account is linked to the Facebook Page
    Given the client's Facebook Page has no linked Instagram account
    When the administrator runs check_instagram_connection.py
    Then FieldKit reports that no Instagram account is linked to the Page
    And the report instructs the administrator to link a Business or Creator account in Meta's Account Settings
    And the script exits with code 3
    And IG_BUSINESS_ACCOUNT_ID is NOT written to .env

  Scenario: Linked Instagram account is a personal account
    Given the client's Facebook Page has a linked Instagram account "@personal_owner"
    And that Instagram account's type is "PERSONAL"
    When the administrator runs check_instagram_connection.py
    Then FieldKit reports that a Business or Creator account is required
    And the report instructs the administrator to convert the account type in the Instagram app
    And the script exits with code 3
    And IG_BUSINESS_ACCOUNT_ID is NOT written to .env

  Scenario: Missing Facebook Page credentials abort setup cleanly
    Given FB_PAGE_ACCESS_TOKEN is not set in .env
    When the administrator runs check_instagram_connection.py
    Then the script exits with code 1
    And an error message indicates the missing configuration

  Scenario: Administrator overrides which Page to check
    Given the account has multiple connected Facebook Pages
    When the administrator runs check_instagram_connection.py with --page-id 123456
    Then FieldKit checks Page ID "123456" for a linked Instagram account
