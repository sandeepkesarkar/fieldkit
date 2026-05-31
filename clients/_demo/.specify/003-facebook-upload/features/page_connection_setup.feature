Feature: Facebook Page Connection Setup
  As an administrator
  I want to generate a Facebook authorization link for a client
  So that the client can grant FieldKit permission to post to their Facebook Page

  Scenario: Authorization link directs owner to Facebook's permission screen
    Given the administrator has configured FB_APP_ID and FB_APP_SECRET in .env
    When the administrator runs generate_auth_link.py
    Then a Facebook authorization URL is printed to stdout
    And the URL requests the "pages_show_list" permission
    And the URL requests the "pages_read_engagement" permission
    And the URL requests the "pages_manage_posts" permission

  Scenario: Successful authorization stores the Page access token
    Given the administrator has generated and shared the authorization URL
    When the owner completes the Facebook authorization flow
    And the OAuth callback is received by the local server
    Then FieldKit exchanges the code for a long-lived user token
    And FieldKit retrieves the permanent Page access token
    And FB_PAGE_ID is written to the client's .env file
    And FB_PAGE_ACCESS_TOKEN is written to the client's .env file
    And the owner sees "Authorization complete. Page access token written to .env."
    And existing .env variables are preserved

  Scenario: Owner with multiple Facebook Pages can select which one to link
    Given the owner has multiple Facebook Pages
    When the administrator runs generate_auth_link.py with --page-id 123456
    Then FieldKit links the Page with ID "123456"
    And FB_PAGE_ID is set to "123456" in .env

  Scenario: Missing app credentials abort setup cleanly
    Given FB_APP_ID is not set in .env
    When the administrator runs generate_auth_link.py
    Then the script exits with code 1
    And an error message indicates the missing configuration

  Scenario: No Facebook Pages found for the account
    Given the authorized account has no Facebook Pages
    When generate_auth_link.py completes the OAuth flow
    Then the script exits with code 3
    And an error message indicates no Pages were found
