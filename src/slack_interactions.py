# src/slack_interactions.py

from flask import Blueprint, request, jsonify
from src.slack_client import SlackClient
from src.api_client import HostawayClient
from src.ai_engine import AIGuestAssistant
from src.config import settings

import json

slack_interactions_bp = Blueprint("slack_interactions", __name__)
slack_client = SlackClient()
hostaway_client = HostawayClient()
ai_assistant = AIGuestAssistant()


@slack_interactions_bp.route("/slack-interactivity", methods=["POST"])
def handle_interactivity():
    """
    Handles interactive components from Slack (buttons, modals, menus).
    Slack sends payloads as form-encoded JSON strings.
    """
    payload = json.loads(request.form.get("payload", "{}"))
    action_id = payload.get("actions", [{}])[0].get("action_id", "")
    user_id = payload.get("user", {}).get("id")

    if not action_id:
        return jsonify({"error": "No action_id"}), 400

    # Debug log
    print(f"[Slack Interactivity] Action triggered: {action_id} by {user_id}")

    # Dispatch based on button or modal action
    if action_id == "send_message":
        return handle_send(payload)
    elif action_id == "edit_message":
        return handle_edit(payload)
    elif action_id == "tone_friendly":
        return handle_tone_change(payload, "friendly")
    elif action_id == "tone_professional":
        return handle_tone_change(payload, "professional")
    elif action_id == "tone_informal":
        return handle_tone_change(payload, "informal")
    elif action_id == "guest_portal":
        return handle_guest_portal(payload)
    else:
        return jsonify({"status": "ignored"}), 200


def handle_send(payload):
    """
    Send message back to Hostaway via API.
    """
    values = payload.get("state", {}).get("values", {})
    conversation_id = payload.get("private_metadata")
    message_text = extract_message_text(values)

    if not message_text or not conversation_id:
        return jsonify({"error": "Missing data"}), 400

    hostaway_client.send_message(conversation_id, message_text)
    slack_client.post_message(
        channel=settings.SLACK_CHANNEL,
        text=f":white_check_mark: Sent reply to guest:\n>>> {message_text}"
    )
    return jsonify({"status": "sent"})


def handle_edit(payload):
    """
    Open a Slack modal to edit or improve the AI-generated message.
    """
    conversation_id = payload.get("private_metadata")
    guest_message = payload.get("message", {}).get("text", "Guest message here")
    ai_suggestion = "Edit this AI suggestion..."

    view = {
        "type": "modal",
        "callback_id": "edit_modal",
        "private_metadata": conversation_id,
        "title": {"type": "plain_text", "text": "Edit Reply"},
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Guest Message:*\n{guest_message}"}
            },
            {
                "type": "input",
                "block_id": "reply_input",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "reply_text",
                    "multiline": True,
                    "initial_value": ai_suggestion
                },
                "label": {"type": "plain_text", "text": "Your Reply"}
            },
            {
                "type": "input",
                "block_id": "improve_input",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "improve_text",
                    "multiline": False
                },
                "label": {"type": "plain_text", "text": "Improve with AI (optional)"}
            }
        ],
        "submit": {"type": "plain_text", "text": "Send"}
    }

    slack_client.open_modal(payload["trigger_id"], view)
    return jsonify({"status": "modal_opened"})


def handle_tone_change(payload, tone):
    """
    Ask AI to re-generate message with a different tone.
    """
    values = payload.get("state", {}).get("values", {})
    conversation_id = payload.get("private_metadata")
    current_text = extract_message_text(values)

    new_text = ai_assistant.generate_reply(f"{current_text}\nMake it more {tone}.")

    slack_client.post_message(
        channel=settings.SLACK_CHANNEL,
        text=f":sparkles: Rewritten in {tone} tone:\n>>> {new_text}"
    )
    return jsonify({"status": "tone_changed"})


def handle_guest_portal(payload):
    """
    Send a guest portal link back to the conversation.
    """
    conversation_id = payload.get("private_metadata")
    portal_url = f"https://guestportal.example.com/{conversation_id}"

    hostaway_client.send_message(conversation_id, f"Here’s your guest portal: {portal_url}")
    slack_client.post_message(
        channel=settings.SLACK_CHANNEL,
        text=f":link: Guest portal sent → {portal_url}"
    )
    return jsonify({"status": "guest_portal_sent"})


def extract_message_text(values):
    """
    Safely extracts text input from Slack modal submission.
    """
    for block in values.values():
        for action in block.values():
            if "value" in action:
                return action["value"]
    return None
