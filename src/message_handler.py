# src/message_handler.py

from src.api_client import HostawayAPI
from src.slack_client import SlackClient
from src.ai_engine import AIAssistant
from src.config import (
    SLACK_CHANNEL,
)

class MessageHandler:
    """
    Coordinates flow between Hostaway (guest messages),
    AI engine (insights + reply), and Slack (UI for the host).
    """

    def __init__(self, api_client: HostawayAPI, slack_client: SlackClient, ai_engine: AIAssistant):
        self.api_client = api_client
        self.slack_client = slack_client
        self.ai_engine = ai_engine

    def handle_new_guest_message(self, conversation_id: int, message_body: str):
        """
        Main entrypoint for handling a new incoming guest message.
        1. Get reservation/conversation context from Hostaway.
        2. Analyze message with AI.
        3. Push formatted message + suggested reply into Slack.
        """

        # Step 1: Fetch reservation/conversation context
        try:
            conversation = self.api_client.get_conversation(conversation_id)
            reservation = conversation.get("Reservation", {})
            guest_name = reservation.get("guestName", "Unknown Guest")
            arrival = reservation.get("arrivalDate")
            departure = reservation.get("departureDate")
            price = reservation.get("totalPrice")
            currency = reservation.get("currency")
            listing_name = reservation.get("listingName", "Unknown Property")
        except Exception as e:
            print(f"[ERROR] Failed to fetch conversation {conversation_id}: {e}")
            return

        # Step 2: Run AI analysis
        try:
            insights = self.ai_engine.analyze_message(message_body)
            reply = self.ai_engine.generate_reply(message_body, reservation)
        except Exception as e:
            print(f"[ERROR] AI analysis failed: {e}")
            return

        # Step 3: Format Slack message
        header = f"*New message from {guest_name}*"
        context = (
            f"🏡 *Property:* {listing_name}\n"
            f"📅 *Stay:* {arrival} → {departure}\n"
            f"💵 *Price:* {price} {currency}"
        )
        body = f"> {message_body}"
        insights_text = (
            f"*Mood:* {insights.get('mood')}\n"
            f"*Occasion:* {insights.get('occasion')}\n"
            f"*Summary:* {insights.get('summary')}"
        )
        suggested_reply = f"*Suggested Reply:*\n{reply}"

        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": f"💬 Guest Message"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": header}},
            {"type": "section", "text": {"type": "mrkdwn", "text": context}},
            {"type": "section", "text": {"type": "mrkdwn", "text": body}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": insights_text}},
            {"type": "section", "text": {"type": "mrkdwn", "text": suggested_reply}},
            {
                "type": "actions",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Send"}, "value": "send"},
                    {"type": "button", "text": {"type": "plain_text", "text": "Edit"}, "value": "edit"},
                    {"type": "button", "text": {"type": "plain_text", "text": "Guest Portal"}, "value": "guest_portal"},
                    {"type": "button", "text": {"type": "plain_text", "text": "More Professional"}, "value": "tone_professional"},
                    {"type": "button", "text": {"type": "plain_text", "text": "More Friendly"}, "value": "tone_friendly"},
                    {"type": "button", "text": {"type": "plain_text", "text": "More Informal"}, "value": "tone_informal"},
                ]
            },
        ]

        # Step 4: Push to Slack
        self.slack_client.send_message(SLACK_CHANNEL, blocks=blocks)
