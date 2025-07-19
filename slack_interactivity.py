# slack_interactivity.py

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

waiting_threads = {}

@router.post("/slack/events")
async def slack_events(request: Request):
    body = await request.body()
    try:
        logging.info(f"Received Slack event: {body.decode()}")
    except Exception:
        logging.info("Received Slack event (non-decodable).")
    if not signature_verifier.is_valid_request(body, request.headers):
        logging.warning("Invalid Slack signature on /slack/events")
        return JSONResponse(status_code=403, content={"status": "invalid signature"})

    try:
        payload = json.loads(body)
    except Exception as e:
        logging.error(f"Error parsing event JSON: {e}")
        return JSONResponse(status_code=400, content={"error": "Invalid JSON", "details": str(e)})

    if "challenge" in payload:
        return JSONResponse(content={"challenge": payload["challenge"]})

    event = payload.get("event", {})
    event_type = event.get("type")

    if event_type == "message" and not event.get("bot_id"):
        thread_ts = event.get("thread_ts")
        user_message_ts = event.get("ts")
        channel = event.get("channel")
        text = event.get("text", "")

        logging.info(f"User message detected in channel={channel}, thread_ts={thread_ts}, message_ts={user_message_ts}")
        logging.info(f"waiting_threads: {waiting_threads}")

        if thread_ts in waiting_threads:
            mode = waiting_threads.pop(thread_ts)
            conv_id, comm_type = None, "channel"
            if isinstance(mode, dict):
                conv_id = mode.get("conv_id")
                comm_type = mode.get("type", "channel")
            logging.info(f"Posting action buttons in response to user reply in thread {thread_ts}, mode={mode}")

            blocks = [
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "📨 Send"},
                            "value": json.dumps({
                                "draft": text,
                                "thread_ts": thread_ts,
                                "channel": channel,
                                "conv_id": conv_id,
                                "type": comm_type
                            }),
                            "action_id": "send"
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✏️ Improve with AI"},
                            "value": json.dumps({
                                "draft": text,
                                "thread_ts": thread_ts,
                                "channel": channel,
                                "conv_id": conv_id,
                                "type": comm_type
                            }),
                            "action_id": "improve"
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✏️ Edit"},
                            "value": json.dumps({
                                "draft": text,
                                "thread_ts": thread_ts,
                                "channel": channel,
                                "conv_id": conv_id,
                                "type": comm_type
                            }),
                            "action_id": "edit"
                        }
                    ]
                }
            ]
            try:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=user_message_ts,
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

    # Modal or block action
    action = payload["actions"][0] if "actions" in payload else None
    action_id = action.get("action_id") if action else None
    value = json.loads(action["value"]) if action else {}
    channel = payload.get("channel", {}).get("id") or value.get("channel")
    thread_ts = value.get("thread_ts") or payload.get("message", {}).get("ts") or payload.get("container", {}).get("thread_ts")

    # Modal trigger
    trigger_id = payload.get("trigger_id")

    # Import here to avoid circular import issues
    from openai import OpenAI
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    openai_client = OpenAI(api_key=OPENAI_API_KEY)

    # --- Modal for Edit ---
    if action_id == "edit":
        draft = value.get("draft")
        conv_id = value.get("conv_id")
        comm_type = value.get("type", "channel")
        # Open a modal for editing
        modal_view = {
            "type": "modal",
            "callback_id": "edit_modal_submit",
            "private_metadata": json.dumps({
                "conv_id": conv_id,
                "type": comm_type,
                "channel": channel,
                "thread_ts": thread_ts
            }),
            "title": {"type": "plain_text", "text": "Edit Reply"},
            "submit": {"type": "plain_text", "text": "Send"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "edit_block",
                    "label": {"type": "plain_text", "text": "Edit the reply below before sending:"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "edit_action",
                        "multiline": True,
                        "initial_value": draft or ""
                    }
                }
            ]
        }
        try:
            client.views_open(trigger_id=trigger_id, view=modal_view)
            logging.info("Modal opened for editing.")
        except Exception as e:
            logging.error(f"Error opening modal: {e}")
        return {}

    # --- Modal submission handler ---
    if payload.get("type") == "view_submission" and payload.get("view", {}).get("callback_id") == "edit_modal_submit":
        view = payload["view"]
        state = view["state"]["values"]
        edit_block = state["edit_block"]["edit_action"]["value"]
        metadata = json.loads(view["private_metadata"])
        conv_id = metadata["conv_id"]
        comm_type = metadata.get("type", "channel")
        channel = metadata.get("channel")
        thread_ts = metadata.get("thread_ts")

        # Send reply to Hostaway
        success = False
        if conv_id:
            success = send_reply_to_hostaway(conv_id, edit_block, comm_type)
        else:
            logging.error("No conv_id in modal submit for Hostaway.")
        try:
            # Post confirmation in Slack thread
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="✅ Edited reply sent to Hostaway!" if success else "❌ Error sending reply to Hostaway."
            )
            logging.info("Modal submission: reply sent to Hostaway.")
        except Exception as e:
            logging.error(f"Error posting modal confirmation: {e}")
        # Acknowledge modal submission (empty body)
        return JSONResponse(content={})

    # --- Write Own (same as before, no modal) ---
    if action_id == "write_own":
        waiting_threads[thread_ts] = {
            "action_id": action_id,
            "conv_id": value.get("conv_id"),
            "type": value.get("type", "channel"),
        }
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

    # --- Send & Improve (same as before) ---
    elif action_id == "send":
        reply = value.get("reply") or value.get("draft")
        conv_id = value.get("conv_id")
        comm_type = value.get("type", "channel")
        success = False
        if conv_id:
            success = send_reply_to_hostaway(conv_id, reply, comm_type)
        else:
            logging.error("No conv_id present for sending reply to Hostaway.")

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
        conv_id = value.get("conv_id")
        comm_type = value.get("type", "channel")
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
                        "value": json.dumps({
                            "reply": improved,
                            "thread_ts": thread_ts,
                            "channel": channel,
                            "conv_id": conv_id,
                            "type": comm_type
                        }),
                        "action_id": "send"
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✏️ Edit"},
                        "value": json.dumps({
                            "draft": improved,
                            "thread_ts": thread_ts,
                            "channel": channel,
                            "conv_id": conv_id,
                            "type": comm_type
                        }),
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
