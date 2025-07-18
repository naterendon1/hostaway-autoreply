from fastapi import APIRouter, Request
import os
import json
import logging
from slack_sdk.web import WebClient
from slack_sdk.signature import SignatureVerifier

router = APIRouter()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
client = WebClient(token=SLACK_BOT_TOKEN)
signature_verifier = SignatureVerifier(SLACK_SIGNING_SECRET)

# Track which threads are expecting a user message for a reply or edit
# You should use a more persistent store (like Redis) for production
waiting_threads = {}

@router.post("/slack/events")
async def slack_events(request: Request):
    body = await request.body()
    if not signature_verifier.is_valid_request(body, request.headers):
        return {"status": "invalid signature"}

    payload = json.loads(body)

    # Slack URL Verification (challenge)
    if "challenge" in payload:
        return payload

    # Handle events
    event = payload.get("event", {})
    event_type = event.get("type")

    # Only handle user messages (not bot messages, not message_changed, etc.)
    if event_type == "message" and not event.get("bot_id"):
        thread_ts = event.get("thread_ts") or event.get("ts")
        user_id = event.get("user")
        text = event.get("text", "")
        channel = event.get("channel")

        # Only proceed if this thread was flagged as waiting for user input
        if thread_ts in waiting_threads:
            mode = waiting_threads.pop(thread_ts)
            logging.info(f"Detected user message in thread {thread_ts}, mode: {mode}")

            # Reply under user's message with buttons
            actions = [
                {
                    "name": "send",
                    "text": "📨 Send",
                    "type": "button",
                    "value": json.dumps({"draft": text})
                },
                {
                    "name": "improve",
                    "text": "✏️ Improve with AI",
                    "type": "button",
                    "value": json.dumps({"draft": text})
                }
            ]
            blocks = [
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "📨 Send"},
                            "value": json.dumps({"draft": text}),
                            "action_id": "send"
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✏️ Improve with AI"},
                            "value": json.dumps({"draft": text}),
                            "action_id": "improve"
                        }
                    ]
                }
            ]
            client.chat_postMessage(
                channel=channel,
                thread_ts=event.get("ts"),
                text="Choose what to do with your draft reply:",
                attachments=[{
                    "callback_id": thread_ts,
                    "color": "#3AA3E3",
                    "attachment_type": "default",
                    "actions": actions
                }]
            )
        else:
            logging.info("User message received in thread with no waiting state; ignoring.")

    # Listen for "write_own" or "edit" button clicks and flag the thread as waiting
    if payload.get("type") == "interactive_message":
        actions = payload.get("actions", [])
        if actions:
            action = actions[0]
            action_name = action.get("name")
            thread_ts = payload.get("thread_ts") or payload.get("message_ts")
            if action_name in ["write_own", "edit"]:
                waiting_threads[thread_ts] = action_name
                channel = payload.get("channel", {}).get("id")
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text="📝 Please type your reply as a message in this thread.\n\n(Once sent, buttons will appear below your message.)"
                )
    return {"status": "ok"}
