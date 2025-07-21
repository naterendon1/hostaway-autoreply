# slack_interactivity.py

import os
import logging
import json
from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from utils import (
    send_reply_to_hostaway,
    fetch_hostaway_resource,
    store_learning_example,
    get_similar_learning_examples
)
from openai import OpenAI

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY)

router = APIRouter()

# Consistent system prompt!
SYSTEM_PROMPT = (
    "You are a highly knowledgeable, super-friendly vacation rental host for homes in Crystal Beach, TX, Austin, TX, Galveston, TX, and Georgetown, TX. "
    "Greet the guest with their first name in a laid-back way—no loud or exaggerated greetings. Keep it casual, concise, and approachable. Never be formal. "
    "Never guess if you don’t know the answer—say you’ll get back to them if you’re unsure. "
    "For restaurant/local recs, use web search if possible and use the property address. "
    "Reference the property details (address, summary, amenities, house manual, etc) for all answers if relevant. "
    "If a guest is only inquiring about dates or making a request, always check the calendar to confirm availability before agreeing. "
    "If the guest already has a confirmed booking, do not check the calendar or mention availability—just answer their questions as they are already booked. "
    "For early check-in or late check-out requests, check if available first, then mention a fee applies. "
    "For refund requests outside the cancellation policy, politely explain that refunds are only possible if the dates rebook. "
    "If a guest cancels for an emergency, show empathy and refer to Airbnb’s extenuating circumstances policy or the relevant platform's version. "
    "For amenity/house details, answer directly with no extra fluff. "
    "For parking, clarify how many vehicles are allowed and where to park (driveways, not blocking neighbors, etc). "
    "For tech/amenity questions (WiFi, TV, grill, etc.), give quick, direct instructions. "
    "If you have a previously saved answer for this question and house, use that wording if appropriate. "
    "Always be helpful and accurate."
)

def get_property_info(listing_id):
    """Fetch listing details, house manual, etc, for property context."""
    property_info = ""
    if not listing_id:
        return property_info
    listing = fetch_hostaway_resource("listings", listing_id)
    result = listing.get("result", {}) if listing else {}
    address = result.get("address", "")
    city = result.get("city", "")
    zipcode = result.get("zip", "")
    summary = result.get("summary", "")
    amenities = result.get("amenities", "")
    house_manual = result.get("houseManual", "")
    if isinstance(amenities, list):
        amenities_str = ", ".join(amenities)
    elif isinstance(amenities, dict):
        amenities_str = ", ".join([k for k, v in amenities.items() if v])
    else:
        amenities_str = str(amenities)
    property_info = (
        f"Property Address: {address}, {city} {zipcode}\n"
        f"Summary: {summary}\n"
        f"Amenities: {amenities_str}\n"
        f"House Manual: {house_manual[:800]}{'...' if len(house_manual) > 800 else ''}\n"
    )
    return property_info

@router.post("/slack/actions")
async def slack_actions(request: Request, background_tasks: BackgroundTasks):
    payload = await request.form()
    payload = json.loads(payload["payload"])
    logging.info(f"Slack action payload: {json.dumps(payload, indent=2)}")
    response_url = payload.get("response_url")
    user = payload.get("user", {})
    guest_id = user.get("id") or ""
    actions = payload.get("actions", [])
    action = actions[0] if actions else {}
    action_id = action.get("action_id")
    value = action.get("value")
    private_metadata = payload.get("view", {}).get("private_metadata", None)

    # Unpack modal state if needed
    def get_modal_state():
        view = payload.get("view", {})
        state = view.get("state", {}).get("values", {})
        reply_input = state.get("reply_input", {}).get("reply", {})
        user_text = reply_input.get("value", "")
        meta = json.loads(view.get("private_metadata", "{}"))
        return user_text, meta

    # --- WRITE YOUR OWN ---
    if action_id == "write_own":
        meta = json.loads(value)
        conv_id = meta.get("conv_id")
        listing_id = meta.get("listing_id", None)
        guest_message = meta.get("guest_message", "")
        type_ = meta.get("type")
        # Open a modal to let user write their reply
        modal = {
            "type": "modal",
            "callback_id": "write_own_modal",
            "title": {"type": "plain_text", "text": "Write Your Own Reply", "emoji": True},
            "submit": {"type": "plain_text", "text": "Send", "emoji": True},
            "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
            "private_metadata": json.dumps({
                "conv_id": conv_id,
                "listing_id": listing_id,
                "guest_message": guest_message,
                "type": type_,
                "thread_ts": payload.get("message", {}).get("ts"),
                "guest_id": guest_id
            }),
            "blocks": [
                {
                    "type": "input",
                    "block_id": "reply_input",
                    "label": {"type": "plain_text", "text": "Your reply:", "emoji": True},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "reply",
                        "multiline": True
                    }
                },
                {
                    "type": "actions",
                    "block_id": "improve_ai_block",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": "improve_with_ai",
                            "text": {"type": "plain_text", "text": ":rocket: Improve with AI", "emoji": True}
                        }
                    ]
                }
            ]
        }
        return JSONResponse({"response_action": "push", "view": modal})

    # --- EDIT BUTTON ---
    if action_id == "edit":
        meta = json.loads(value)
        conv_id = meta.get("conv_id")
        listing_id = meta.get("listing_id", None)
        guest_message = meta.get("guest_message", "")
        draft = meta.get("draft", "")
        type_ = meta.get("type")
        modal = {
            "type": "modal",
            "callback_id": "edit_reply_modal",
            "title": {"type": "plain_text", "text": "Edit Reply", "emoji": True},
            "submit": {"type": "plain_text", "text": "Send", "emoji": True},
            "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
            "private_metadata": json.dumps({
                "conv_id": conv_id,
                "listing_id": listing_id,
                "guest_message": guest_message,
                "type": type_,
                "thread_ts": payload.get("message", {}).get("ts"),
                "guest_id": guest_id
            }),
            "blocks": [
                {
                    "type": "input",
                    "block_id": "reply_input",
                    "label": {"type": "plain_text", "text": "Edit your reply:", "emoji": True},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "reply",
                        "multiline": True,
                        "initial_value": draft
                    }
                },
                {
                    "type": "actions",
                    "block_id": "improve_ai_block",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": "improve_with_ai",
                            "text": {"type": "plain_text", "text": ":rocket: Improve with AI", "emoji": True}
                        }
                    ]
                }
            ]
        }
        return JSONResponse({"response_action": "push", "view": modal})

    # --- VIEW SUBMISSION (WRITE OWN OR EDIT) ---
    if payload.get("type") == "view_submission":
        user_reply, meta = get_modal_state()
        conv_id = meta.get("conv_id")
        listing_id = meta.get("listing_id")
        guest_message = meta.get("guest_message", "")
        type_ = meta.get("type")
        guest_id = meta.get("guest_id")
        ai_suggestion = meta.get("ai_suggestion", "")
        # Save to learning
        store_learning_example(guest_message, ai_suggestion, user_reply, listing_id, guest_id)
        # Send reply to Hostaway
        send_ok = send_reply_to_hostaway(conv_id, user_reply, type_)
        msg = ":white_check_mark: Your reply was sent and saved for future learning!" if send_ok else ":warning: Failed to send."
        return JSONResponse({
            "response_action": "clear",
            "view": {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Done!", "emoji": True},
                "close": {"type": "plain_text", "text": "Close", "emoji": True},
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": msg}
                    }
                ]
            }
        })

    # --- IMPROVE WITH AI BUTTON ---
    if action_id == "improve_with_ai":
        # Grab the modal's state so far
        view = payload.get("view", {})
        state = view.get("state", {}).get("values", {})
        reply_block = state.get("reply_input", {})
        edited_text = None
        for v in reply_block.values():
            edited_text = v.get("value")
        meta = json.loads(view.get("private_metadata", "{}"))
        guest_message = meta.get("guest_message", "")
        listing_id = meta.get("listing_id", None)
        guest_id = meta.get("guest_id", None)
        ai_suggestion = meta.get("ai_suggestion", "")

        # Retrieve property info for this listing
        property_info = get_property_info(listing_id)

        # Find previous similar learning examples for this house/question
        similar_examples = get_similar_learning_examples(guest_message, listing_id)
        prev_answer = ""
        if similar_examples:
            prev_answer = f"Previously, you (the host) replied to a similar guest question about this property: \"{similar_examples[0][2]}\". Use this as a guide if it fits.\n"

        # Compose prompt using the same method as /unified-webhook
        prompt = (
            f"{prev_answer}"
            f"A guest sent this message:\n{guest_message}\n\n"
            f"Property info:\n{property_info}\n"
            "Respond according to your latest rules and tone, and use property info to make answers detailed and specific if appropriate. "
            "If you don't know the answer from the details provided, say you will check and get back to them. "
            f"Here’s my draft, please improve it for clarity and tone, but do NOT make up info you can't find in the property details or previous answers:\n"
            f"{edited_text}"
        )
        try:
            response = openai_client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ]
            )
            improved = response.choices[0].message.content.strip()
        except Exception as e:
            logging.error(f"OpenAI error in 'improve_with_ai': {e}")
            improved = "(Error generating improved reply.)"

        # Return new modal with improved answer in the text box (replace input)
        new_modal = view.copy()
        # Overwrite the reply value in blocks for improved text
        new_blocks = []
        for block in new_modal["blocks"]:
            if block["type"] == "input" and block["block_id"] == "reply_input":
                block["element"]["initial_value"] = improved
            new_blocks.append(block)
        new_modal["blocks"] = new_blocks

        return JSONResponse({"response_action": "update", "view": new_modal})

    # --- SEND BUTTON (from main message, not modal) ---
    if action_id == "send":
        meta = json.loads(value)
        conv_id = meta.get("conv_id")
        reply = meta.get("reply")
        type_ = meta.get("type")
        listing_id = meta.get("listing_id", None)
        guest_message = meta.get("guest_message", "")
        guest_id = meta.get("guest_id", None)
        ai_suggestion = reply
        # Store as learning (AI suggestion, not user-modified)
        store_learning_example(guest_message, ai_suggestion, reply, listing_id, guest_id)
        send_ok = send_reply_to_hostaway(conv_id, reply, type_)
        msg = ":white_check_mark: AI reply sent and saved!" if send_ok else ":warning: Failed to send."
        # Optionally, update the Slack message here to show "Sent!"
        return JSONResponse({"text": msg})

    # Default: just acknowledge
    return JSONResponse({"text": "Action received."})
