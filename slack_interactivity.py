from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import os
import json
import logging
from slack_sdk.web import WebClient
from slack_sdk.signature import SignatureVerifier
from utils import send_reply_to_hostaway

router = APIRouter()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
client = WebClient(token=SLACK_BOT_TOKEN)
signature_verifier = SignatureVerifier(SLACK_SIGNING_SECRET)

# In-memory state; for production use, switch to Redis or DB
waiting_threads = {}

@router.post("/slack/events")
async def slack_events(request: Request):
    body = await request.body()
    try:
        logging.info(f"Received Slack event: {body.decode()}")
    except Exception:
        logging.info("Received Slack event (non-decodable).")
    # Signature verification
    if not signature_verifier.is_valid_request(body, request.headers):
        logging.warning("Invalid Slack signature on /slack/events")
        return JSONResponse(status_code=403, content={"status": "invalid signature"})

    try:
        payload = json.loads(body)
    except Exception as e:
        logging.error(f"Error parsing event JSON: {e}")
        return JSONResponse(status_code=400, content={"error": "Invalid JSON", "details": str(e)})

    # Slack URL verification challenge
    if "challenge" in payload:
        return JSONResponse(content={"challenge": payload["challenge"]})

    event = payload.get("event", {})
    event_type = event.get("type")

    # Handle user message in thread
    if event_type == "message" and not event.get("bot_id"):
        thread_ts = event.get("thread_ts")    # Parent bot message ts
        user_message_ts = event.get("ts")     # This user's message ts
        channel = event.get("channel")
        text = event.get("text", "")

        logging.info(f"User message detected in channel={channel}, thread_ts={thread_ts}, message_ts={user_message_ts}")

        # Only respond if thread is flagged as waiting
        if thread_ts in waiting_threads:
            mode = waiting_threads.pop(thread_ts)
            logging.info(f"Posting action buttons in response to user reply in thread {thread_ts}, mode={mode}")

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
            try:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=user_message_ts,  # reply directly under user's message
                    text="Choose what to do with your draft reply:",
                    blocks=blocks
                )
                logging.info("Action buttons posted under user message.")
            except Exception as e:
                logging.error(f"Error posting action buttons: {e}")
        else:
            logging.info("User message in thread received but no waiting state found; ignoring.")

    return {"status": "ok"}


@router.post("/slack/actions")
async def slack_actions(request: Request):
    form = await request.form()
    body = form["payload"]
    try:
        payload = json.loads(body)
        logging.info(f"Slack action payload: {json.dumps(payload, indent=2)}")
    except Exception as e:
        logging.error(f"Could not parse action payload: {e}")
        return JSONResponse(status_code=400, content={"error": "Invalid action payload"})

    # Signature verification (optional here, already checked on /slack/events)
    # If you want, you can re-enable below:
    # if not signature_verifier.is_valid_request(await request.body(), request.headers):
    #     logging.error("Slack signature verification failed on /slack/actions.")
    #     return JSONResponse(status_code=403, content={"status": "invalid signature"})

    user = payload.get("user", {}).get("id")
    action = payload["actions"][0]
    action_id = action.get("action_id")
    value = json.loads(action["value"])
    channel = payload.get("channel", {}).get("id")
    thread_ts = value.get("thread_ts") or payload.get("message", {}).get("ts") or payload.get("container", {}).get("thread_ts")

    # Import here to avoid circular import issues
    from openai import OpenAI
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    openai_client = OpenAI(api_key=OPENAI_API_KEY)

    if action_id in ["write_own", "edit"]:
        # Mark thread as waiting for user input
        waiting_threads[thread_ts] = action_id
        try:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="📝 Please type your reply as a message in this thread.\n\n(Once sent, buttons will appear below your message.)"
            )
            logging.info(f"Prompted user to type reply in thread {thread_ts}")
        except Exception as e:
            logging.error(f"Error prompting user to type reply: {e}")
        return {}

    elif action_id == "send":
        reply = value.get("reply") or value.get("draft")
        conv_id = value.get("conv_id", None)
        comm_type = value.get("type", "channel")
        success = send_reply_to_hostaway(conv_id, reply, comm_type)
        try:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="✅ Reply sent to Hostaway!" if success else "❌ Error sending reply to Hostaway."
            )
            logging.info(f"Reply sent to Hostaway: {success}")
        except Exception as e:
            logging.error(f"Error posting Hostaway send result: {e}")
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
        try:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="Here's an improved version. Send or edit?",
                blocks=blocks
            )
            logging.info("Improved reply posted.")
        except Exception as e:
            logging.error(f"Error posting improved reply: {e}")
        return {}

    else:
        logging.warning(f"Unhandled Slack action_id: {action_id}")
        return {}
