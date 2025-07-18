from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import json
import logging
import requests
import os
from openai import OpenAI

router = APIRouter()

HOSTAWAY_API_KEY = os.getenv("HOSTAWAY_ACCESS_TOKEN")
HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

# Utility to send to Hostaway
def send_reply_to_hostaway(conversation_id: str, reply_text: str) -> bool:
    url = f"{HOSTAWAY_API_BASE}/conversations/{conversation_id}/messages"
    headers = {
        "Authorization": f"Bearer {HOSTAWAY_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Cache-Control": "no-cache"
    }
    payload = {
        "body": reply_text,
        "isIncoming": 0,
        "communicationType": "email"
    }
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        logging.info(f"✅ Successfully sent reply. Response: {response.text}")
        return True
    except requests.exceptions.HTTPError as e:
        logging.error(f"❌ HTTPError sending reply: {e.response.status_code} - {e.response.text}")
        return False
    except Exception as e:
        logging.error(f"❌ Unexpected error: {str(e)}")
        return False

# Utility to improve with OpenAI
def improve_with_gpt(draft_text: str) -> str:
    prompt = f"""Rewrite this guest message reply to be friendly, concise, clear, and informal. Do NOT include a signoff:\n\n{draft_text}"""
    try:
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are a helpful, friendly vacation rental host."},
                {"role": "user", "content": prompt}
            ]
        )
        improved = response.choices[0].message.content.strip()
        return improved
    except Exception as e:
        logging.error(f"❌ OpenAI error: {e}")
        return draft_text

@router.post("/slack-interactivity")
async def slack_action(request: Request):
    form_data = await request.form()
    payload = json.loads(form_data["payload"])
    action = payload["actions"][0]
    action_type = action["name"]
    callback_id = payload.get("callback_id")
    value_data = {}

    # Unpack value as JSON if present
    if "value" in action:
        try:
            value_data = json.loads(action["value"])
        except Exception:
            value_data = {"draft": action["value"]}

    # Handle actions
    # 1. Approve (send suggested reply as-is)
    if action_type == "approve":
        reply = value_data.get("reply", "")
        success = send_reply_to_hostaway(callback_id, reply)
        if success:
            return JSONResponse({"text": f"✅ Sent to guest:\n>{reply}", "replace_original": True})
        else:
            return JSONResponse({"text": "❌ Failed to send reply to Hostaway."})

    # 2. Write Your Own
    elif action_type == "write_own":
        return JSONResponse({
            "text": "📝 Please compose your reply as a message in this thread.\n\n*Once you've typed it, click an option below:*",
            "attachments": [
                {
                    "callback_id": callback_id,
                    "fallback": "Compose your reply",
                    "color": "#3AA3E3",
                    "attachment_type": "default",
                    "actions": [
                        {"name": "send", "text": "📨 Send", "type": "button", "value": json.dumps({"draft": ""})},
                        {"name": "improve", "text": "✏️ Improve with AI", "type": "button", "value": json.dumps({"draft": ""})},
                        {"name": "back", "text": "🔙 Back", "type": "button", "value": json.dumps({})}
                    ]
                }
            ]
        })

    # 3. Edit (edit the AI suggestion)
    elif action_type == "edit":
        # Provide the AI draft in a block for editing
        draft = value_data.get("draft", "")
        return JSONResponse({
            "text": f"✏️ *Edit the AI suggestion below (copy, edit, then use a button):*\n\n>{draft}\n\n*Type your changes, then click an option below:*",
            "attachments": [
                {
                    "callback_id": callback_id,
                    "fallback": "Edit reply",
                    "color": "#3AA3E3",
                    "attachment_type": "default",
                    "actions": [
                        {"name": "send", "text": "📨 Send", "type": "button", "value": json.dumps({"draft": draft})},
                        {"name": "improve", "text": "✏️ Improve with AI", "type": "button", "value": json.dumps({"draft": draft})},
                        {"name": "back", "text": "🔙 Back", "type": "button", "value": json.dumps({})}
                    ]
                }
            ]
        })

    # 4. Improve with AI
    elif action_type == "improve":
        draft = value_data.get("draft", "")
        improved = improve_with_gpt(draft)
        return JSONResponse({
            "text": f"🤖 *Improved version:*\n\n>{improved}\n\n*You can now send, edit, or rewrite again:*",
            "attachments": [
                {
                    "callback_id": callback_id,
                    "fallback": "Improve or send",
                    "color": "#3AA3E3",
                    "attachment_type": "default",
                    "actions": [
                        {"name": "send", "text": "📨 Send", "type": "button", "value": json.dumps({"draft": improved})},
                        {"name": "edit", "text": "✏️ Edit", "type": "button", "value": json.dumps({"draft": improved})},
                        {"name": "rewrite_again", "text": "🔁 Rewrite Again", "type": "button", "value": json.dumps({"draft": improved})},
                        {"name": "back", "text": "🔙 Back", "type": "button", "value": json.dumps({})}
                    ]
                }
            ]
        })

    # 5. Rewrite Again (repeat improvement on current draft)
    elif action_type == "rewrite_again":
        draft = value_data.get("draft", "")
        improved = improve_with_gpt(draft)
        return JSONResponse({
            "text": f"🔁 *Another improved version:*\n\n>{improved}\n\n*You can now send, edit, or rewrite again:*",
            "attachments": [
                {
                    "callback_id": callback_id,
                    "fallback": "Rewrite, edit or send",
                    "color": "#3AA3E3",
                    "attachment_type": "default",
                    "actions": [
                        {"name": "send", "text": "📨 Send", "type": "button", "value": json.dumps({"draft": improved})},
                        {"name": "edit", "text": "✏️ Edit", "type": "button", "value": json.dumps({"draft": improved})},
                        {"name": "rewrite_again", "text": "🔁 Rewrite Again", "type": "button", "value": json.dumps({"draft": improved})},
                        {"name": "back", "text": "🔙 Back", "type": "button", "value": json.dumps({})}
                    ]
                }
            ]
        })

    # 6. Send (send current draft to Hostaway)
    elif action_type == "send":
        draft = value_data.get("draft", "")
        success = send_reply_to_hostaway(callback_id, draft)
        if success:
            return JSONResponse({"text": f"✅ Sent to guest:\n>{draft}", "replace_original": True})
        else:
            return JSONResponse({"text": "❌ Failed to send reply to Hostaway."})

    # 7. Back (could restore to original choices, or simply show a message)
    elif action_type == "back":
        return JSONResponse({
            "text": "🔙 Back to main options. Please choose how to reply:",
            "attachments": [
                {
                    "callback_id": callback_id,
                    "fallback": "Back to options",
                    "color": "#3AA3E3",
                    "attachment_type": "default",
                    "actions": [
                        {"name": "approve", "text": "✅ Approve", "type": "button", "value": json.dumps({"reply": ""})},
                        {"name": "edit", "text": "✏️ Edit", "type": "button", "value": json.dumps({"draft": ""})},
                        {"name": "write_own", "text": "📝 Write Your Own", "type": "button", "value": json.dumps({})}
                    ]
                }
            ]
        })

    # Unknown action fallback
    return JSONResponse({"text": "⚠️ Unknown Slack action."})

