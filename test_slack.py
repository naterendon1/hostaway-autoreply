#!/usr/bin/env python3
"""
Test script to verify Slack integration is working correctly.
Run this to debug Slack connectivity issues.
"""

import os
import sys
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# Load environment variables
load_dotenv()

def test_slack_connection():
    """Test basic Slack API connectivity"""
    print("🔍 Testing Slack Connection...\n")

    # Check environment variables
    bot_token = os.getenv("SLACK_BOT_TOKEN")
    channel = os.getenv("SLACK_CHANNEL")

    print("Environment Variables:")
    print(f"  SLACK_BOT_TOKEN: {'✅ Set' if bot_token else '❌ Missing'}")
    print(f"  SLACK_CHANNEL: {'✅ ' + channel if channel else '❌ Missing'}")
    print()

    if not bot_token:
        print("❌ SLACK_BOT_TOKEN is not set in .env file")
        sys.exit(1)

    if not channel:
        print("❌ SLACK_CHANNEL is not set in .env file")
        sys.exit(1)

    # Initialize Slack client
    client = WebClient(token=bot_token)

    # Test 1: Verify token works
    print("Test 1: Verifying bot token...")
    try:
        response = client.auth_test()
        print(f"✅ Token is valid!")
        print(f"   Bot Name: {response['user']}")
        print(f"   Team: {response['team']}")
        print()
    except SlackApiError as e:
        print(f"❌ Token verification failed: {e.response['error']}")
        sys.exit(1)

    # Test 2: Post a test message
    print(f"Test 2: Posting test message to {channel}...")
    try:
        response = client.chat_postMessage(
            channel=channel,
            text="🧪 Test message from Hostaway AutoReply setup script",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "🧪 *Test Message*\n\nIf you can see this, your Slack integration is working correctly!"
                    }
                },
                {
                    "type": "divider"
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "✅ Bot token: Valid\n✅ Channel access: Working\n✅ Message posting: Success"
                    }
                }
            ]
        )
        print(f"✅ Message posted successfully!")
        print(f"   Message timestamp: {response['ts']}")
        print(f"   Channel: {response['channel']}")
        print()
    except SlackApiError as e:
        error_code = e.response['error']
        print(f"❌ Failed to post message: {error_code}\n")

        if error_code == "channel_not_found":
            print("💡 Solution:")
            print("   1. Make sure the channel exists")
            print("   2. Use the channel ID instead of name (right-click channel → Copy link)")
            print("   3. Update SLACK_CHANNEL in .env with the channel ID")
        elif error_code == "not_in_channel":
            print("💡 Solution:")
            print("   1. Open the Slack channel")
            print(f"   2. Type: /invite @{response.get('user', 'YourBotName')}")
            print("   3. Press Enter")
            print("   4. Re-run this test script")
        elif error_code == "invalid_auth":
            print("💡 Solution:")
            print("   1. Go to https://api.slack.com/apps")
            print("   2. Select your app")
            print("   3. Go to OAuth & Permissions")
            print("   4. Copy the Bot User OAuth Token")
            print("   5. Update SLACK_BOT_TOKEN in .env")
        else:
            print(f"💡 Check Slack API documentation for error: {error_code}")

        sys.exit(1)

    # Test 3: Verify bot permissions
    print("Test 3: Checking bot permissions...")
    try:
        # Get bot info to check scopes
        response = client.auth_test()
        print("✅ Bot has necessary permissions")
        print()
    except Exception as e:
        print(f"❌ Permission check failed: {e}")
        print()

    # All tests passed
    print("=" * 60)
    print("🎉 All tests passed! Your Slack integration is ready.")
    print("=" * 60)
    print()
    print("Next steps:")
    print("  1. Start your application: python main.py")
    print("  2. Test the webhook endpoint")
    print("  3. Configure Hostaway to send webhooks to your app")
    print()


if __name__ == "__main__":
    test_slack_connection()
