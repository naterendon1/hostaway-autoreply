# slack_interactivity.py
from fastapi import APIRouter, Request
import os
import json
import logging
from slack_sdk.web import WebClient
from slack_sdk.signature import SignatureVerifier
from main import send_reply_to_hostaway  # import from main

router = APIRouter()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
client = WebClient(token=SLACK_BOT_TOKEN)
signature_verifier = SignatureVerifier(SLACK_SIGNING_SECRET)

# Use a simple in-memory dict for demo, replace with Redis in prod!
waiting_threads = {}

@router.post("/slack/events")
async def slack_events(request: Request):
    body = await request.body()
    if not signature_verifier.is_valid_request(body, request.headers):
        return {"status": "invalid signature"}

    payload = json.loads(body)
    if "challenge" in payload:
        return payload

    event = payload.get("event", {})
    event_type = event.get("type")

    if event_type == "message" and not event.get("bot_id"):
        thread_ts = event.get("thread_ts") or event.get("ts")
        user_id = event.get("user")
        text = event.get("text", "")
        channel = event.get("channel")

        if thread_ts in waiting_threads:
            mode = waiting_threads.pop(thread_ts)
            logging.info(f"Detected user message in thread {thread_ts}, mode: {mode}")

            blocks = [
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "📨 Send"},
                            "value": json.dumps({"draft": text, "thread_ts": thread_ts, "channel": channel}),
                            "action_id": "send"
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✏️ Improve with AI"},
                            "value": json.dumps({"draft": text, "thread_ts": thread_ts, "channel": channel}),
                            "action_id": "improve"
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✏️ Edit"},
                            "value": json.dumps({"thread_ts": thread_ts, "channel": channel}),
                            "action_id": "edit"
                        }
                    ]
                }
            ]
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="Choose what to do with your draft reply:",
                blocks=blocks
            )
        else:
            logging.info("User message received in thread with no waiting state; ignoring.")

    return {"status": "ok"}

@router.post("/slack/actions")
async def slack_actions(request: Request):
    form = await request.form()
    payload = json.loads(form["payload"])
    if not signature_verifier.is_valid_request(request._body, request.headers):
        return {"status": "invalid signature"}

    user = payload.get("user", {}).get("id")
    action = payload["actions"][0]
    action_id = action.get("action_id")
    value = json.loads(action["value"])
    channel = payload.get("channel", {}).get("id")
    thread_ts = payload.get("message", {}).get("ts") or payload.get("container", {}).get("thread_ts")

    from openai import OpenAI
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    openai_client = OpenAI(api_key=OPENAI_API_KEY)

    if action_id in ["write_own", "edit"]:
        waiting_threads[thread_ts] = action_id
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="📝 Please type your reply as a message in this thread.\n\n(Once sent, buttons will appear below your message.)"
        )
        return {}

    elif action_id == "send":
        reply = value.get("reply") or value.get("draft")
        conv_id = value.get("conv_id", None)
        comm_type = value.get("type", "channel")
        success = send_reply_to_hostaway(conv_id, reply, comm_type)
        if success:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="✅ Reply sent to Hostaway!"
            )
        else:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="❌ Error sending reply to Hostaway."
            )
        return {}

    elif action_id == "improve":
        draft = value.get("reply") or value.get("draft")
        try:
            response = openai_client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "You are a helpful, friendly vacation rental host."},
                    {"role": "user", "content": f"Please improve this draft reply: {draft}"}
                ]
            )
            improved = response.choices[0].message.content.strip()
        except Exception as e:
            improved = f"(AI error: {e})"
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Improved Reply:*\n>{improved}"}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ Send"},
                        "value": json.dumps({"reply": improved, "thread_ts": thread_ts, "channel": channel}),
                        "action_id": "send"
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✏️ Edit"},
                        "value": json.dumps({"thread_ts": thread_ts, "channel": channel}),
                        "action_id": "edit"
                    }
                ]
            }
        ]
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="Here's an improved version. Send or edit?",
            blocks=blocks
        )
        return {}

    else:
        return {}

