# slack_interactivity.py

import os
import json
import logging
from fastapi import APIRouter, Request, Response
from slack_sdk.web import WebClient
from utils import send_reply_to_hostaway, fetch_hostaway_resource

import sqlite3

# --- Database init (simple) ---
DB_FILE = "learning_examples.db"
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS learning_examples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT,
            answer TEXT,
            listing_id TEXT,
            guest_id TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
init_db()

def store_learning_example(question, answer, listing_id, guest_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO learning_examples (question, answer, listing_id, guest_id) VALUES (?, ?, ?, ?)",
        (question, answer, listing_id, guest_id)
    )
    conn.commit()
    conn.close()

# --- Router ---
router = APIRouter()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL")
slack_client = WebClient(token=SLACK_BOT_TOKEN)

# Helper to get values safely from Slack payloads
def get_action_value(action, key, default=None):
    try:
        return json.loads(action.get("value", "{}")).get(key, default)
    except Exception:
        return default

@router.post("/slack/actions")
async def slack_actions(request: Request):
    payload = await request.form()
    payload = json.loads(payload.get("payload", "{}"))
    logging.info(f"Slack action payload: {json.dumps(payload, indent=2)}")

    # Modal submit (write your own / edit)
    if payload.get("type") == "view_submission":
        private_metadata = json.loads(payload["view"]["private_metadata"])
        reply_text = payload["view"]["state"]["values"]["reply_input"]["reply"]["value"]
        conv_id = private_metadata.get("conv_id")
        listing_id = private_metadata.get("listing_id")
        guest_message = private_metadata.get("guest_message")
        guest_id = private_metadata.get("guest_id")
        communication_type = private_metadata.get("type", "channel")
        thread_ts = private_metadata.get("thread_ts")

        # Send to Hostaway
        send_reply_to_hostaway(conv_id, reply_text, communication_type)
        # Store as learning example
        if guest_message:
            store_learning_example(guest_message, reply_text, listing_id, guest_id)

        # Post to Slack: Your Reply + Improve with AI + Edit
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Your Reply:*\n>{reply_text}"}
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✏️ Improve with AI"},
                        "value": json.dumps({
                            "reply": reply_text,
                            "conv_id": conv_id,
                            "type": communication_type,
                            "listing_id": listing_id,
                            "guest_id": guest_id,
                            "thread_ts": thread_ts,
                            "guest_message": guest_message,
                        }),
                        "action_id": "improve_with_ai"
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✏️ Edit"},
                        "value": json.dumps({
                            "draft": reply_text,
                            "conv_id": conv_id,
                            "type": communication_type,
                            "listing_id": listing_id,
                            "guest_id": guest_id,
                            "thread_ts": thread_ts,
                            "guest_message": guest_message,
                        }),
                        "action_id": "edit"
                    }
                ]
            }
        ]
        slack_client.chat_postMessage(
            channel=SLACK_CHANNEL,
            thread_ts=thread_ts,
            blocks=blocks,
            text="Your custom reply"
        )

        # Respond to Slack (modal close)
        return Response(status_code=200, content=json.dumps({
            "response_action": "clear"
        }), media_type="application/json")

    # Interactive message actions
    if payload.get("type") == "block_actions":
        action = payload["actions"][0]
        action_id = action.get("action_id")
        value = json.loads(action.get("value", "{}"))
        conv_id = value.get("conv_id")
        reply = value.get("reply", "")
        draft = value.get("draft", "")
        communication_type = value.get("type", "channel")
        listing_id = value.get("listing_id")
        guest_id = value.get("guest_id")
        guest_message = value.get("guest_message")
        thread_ts = value.get("thread_ts")

        # --- Improve with AI button ---
        if action_id == "improve_with_ai":
            # Fetch property info for AI context
            prop_info = ""
            if listing_id:
                listing = fetch_hostaway_resource("listings", listing_id)
                result = listing.get("result", {}) if listing else {}
                address = result.get("address", "")
                city = result.get("city", "")
                zipcode = result.get("zip", "")
                summary = result.get("summary", "")
                amenities = result.get("amenities", "")
                if isinstance(amenities, list):
                    amenities_str = ", ".join(amenities)
                elif isinstance(amenities, dict):
                    amenities_str = ", ".join([k for k, v in amenities.items() if v])
                else:
                    amenities_str = str(amenities)
                prop_info = (
                    f"Address: {address}, {city} {zipcode}\n"
                    f"Summary: {summary}\n"
                    f"Amenities: {amenities_str}\n"
                )

            # System prompt (keep in sync with main.py!)
            system_prompt = (
                "You are a super-friendly, knowledgeable vacation rental host for homes in Texas. "
                "Improve the draft reply below for clarity, tone, and accuracy. Make it informal and guest-focused, using the listing info. "
                "Don't make up info. If you don't know, say you'll get back with an answer."
            )

            user_prompt = (
                f"Guest's message:\n{guest_message}\n\n"
                f"Property info:\n{prop_info}\n\n"
                f"Draft reply:\n{reply}\n\n"
                "Improve this reply for the guest."
            )

            # Call OpenAI
            from openai import OpenAI
            OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
            openai_client = OpenAI(api_key=OPENAI_API_KEY)
            try:
                ai_response = openai_client.chat.completions.create(
                    model="gpt-4",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ]
                )
                improved_reply = ai_response.choices[0].message.content.strip()
            except Exception as e:
                logging.error(f"OpenAI error: {e}")
                improved_reply = "(Sorry, I couldn't improve the reply right now.)"

            # Post improved reply with buttons (Send, Edit)
            blocks = [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*AI Improved Reply:*\n>{improved_reply}"}
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✅ Send"},
                            "value": json.dumps({
                                "reply": improved_reply,
                                "conv_id": conv_id,
                                "type": communication_type,
                                "listing_id": listing_id,
                                "guest_id": guest_id,
                                "thread_ts": thread_ts,
                                "guest_message": guest_message,
                            }),
                            "action_id": "send"
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✏️ Edit"},
                            "value": json.dumps({
                                "draft": improved_reply,
                                "conv_id": conv_id,
                                "type": communication_type,
                                "listing_id": listing_id,
                                "guest_id": guest_id,
                                "thread_ts": thread_ts,
                                "guest_message": guest_message,
                            }),
                            "action_id": "edit"
                        }
                    ]
                }
            ]
            slack_client.chat_postMessage(
                channel=SLACK_CHANNEL,
                thread_ts=thread_ts,
                blocks=blocks,
                text="AI improved reply"
            )
            return Response(status_code=200)

        # --- Edit button: open modal with text pre-filled ---
        if action_id == "edit":
            modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Edit Reply", "emoji": True},
                "callback_id": "edit_modal",
                "private_metadata": json.dumps({
                    "conv_id": conv_id,
                    "listing_id": listing_id,
                    "guest_message": guest_message,
                    "guest_id": guest_id,
                    "type": communication_type,
                    "thread_ts": thread_ts
                }),
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "reply_input",
                        "label": {"type": "plain_text", "text": "Edit your reply:", "emoji": True},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "reply",
                            "initial_value": draft,
                            "multiline": True
                        }
                    }
                ]
            }
            trigger_id = payload.get("trigger_id")
            slack_client.views_open(trigger_id=trigger_id, view=modal)
            return Response(status_code=200)

        # --- Write Your Own button: open modal with empty ---
        if action_id == "write_own":
            modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Write Your Own Reply", "emoji": True},
                "callback_id": "write_own_modal",
                "private_metadata": json.dumps({
                    "conv_id": conv_id,
                    "listing_id": listing_id,
                    "guest_message": guest_message,
                    "guest_id": guest_id,
                    "type": communication_type,
                    "thread_ts": thread_ts
                }),
                "submit": {"type": "plain_text", "text": "Send", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "reply_input",
                        "label": {"type": "plain_text", "text": "Your reply:", "emoji": True},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "reply",
                            "initial_value": "",
                            "multiline": True
                        }
                    }
                ]
            }
            trigger_id = payload.get("trigger_id")
            slack_client.views_open(trigger_id=trigger_id, view=modal)
            return Response(status_code=200)

        # --- Send button: send reply to Hostaway & store learning example ---
        if action_id == "send":
            send_reply_to_hostaway(conv_id, reply, communication_type)
            if guest_message:
                store_learning_example(guest_message, reply, listing_id, guest_id)
            # Confirmation ephemeral message
            return Response(
                status_code=200,
                content=json.dumps({
                    "response_type": "ephemeral",
                    "text": ":white_check_mark: Your reply was sent and saved for future learning!"
                }),
                media_type="application/json"
            )

    # Default: just 200 OK
    return Response(status_code=200)

