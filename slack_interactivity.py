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

# --- Cleaner for AI replies (removes sign-offs, extra line breaks, etc) ---
def clean_ai_reply(reply: str) -> str:
    bad_signoffs = [
        "Enjoy your meal", "Enjoy your meals", "Enjoy!", "Best,", "Best regards,", "Cheers,", "Sincerely,", "[Your Name]", "Best", "Sincerely"
    ]
    for signoff in bad_signoffs:
        if signoff in reply:
            reply = reply.replace(signoff, "")
    lines = reply.split('\n')
    filtered_lines = []
    for line in lines:
        stripped = line.strip()
        if any(stripped.startswith(s.replace(",", "")) for s in ["Best", "Cheers", "Sincerely"]):
            continue
        if "[Your Name]" in stripped:
            continue
        filtered_lines.append(line)
    reply = ' '.join(filtered_lines)
    return reply.strip().replace("  ", " ").rstrip(",. ")

# --- Updated SYSTEM PROMPT (strictly concise, casual, no sign-off) ---
SYSTEM_PROMPT = (
    "You are a vacation rental host for homes in Crystal Beach, TX, Austin, TX, Galveston, TX, and Georgetown, TX. "
    "Answer guest questions in a concise, informal, and polite way, always to-the-point. "
    "Never add extra information, suggestions, or local tips unless the guest asks for it. "
    "Do not include fluff, chit-chat, upsell, or overly friendly phrases. "
    "Never use sign-offs like 'Enjoy your meal', 'Enjoy your meals', 'Enjoy!', 'Best', or your name. Only use a simple closing like 'Let me know if you need anything else' or 'Let me know if you need more recommendations' if it’s natural for the situation, and leave it off entirely if the message already feels complete. "
    "Never use multi-line replies unless absolutely necessary—keep replies to a single paragraph with greeting and answer together. "
    "Greet the guest casually using their first name if known, then answer their question immediately. "
    "If you don’t know the answer, say you’ll check and get back to them. "
    "If a guest is only inquiring about dates or making a request, always check the calendar to confirm availability before agreeing. "
    "If the guest already has a confirmed booking, do not check the calendar or mention availability—just answer their questions directly. "
    "For early check-in or late check-out requests, check if available first, then mention a fee applies. "
    "For refund requests outside the cancellation policy, politely explain that refunds are only possible if the dates rebook. "
    "If a guest cancels for an emergency, show empathy and refer to Airbnb’s extenuating circumstances policy or the relevant platform's version. "
    "For amenity/house details, answer directly with no extra commentary. "
    "For parking, clarify how many vehicles are allowed and where to park (driveways, not blocking neighbors, etc). "
    "For tech/amenity questions (WiFi, TV, grill, etc.), give quick, direct instructions. "
    "If you have a previously saved answer for this question and house, use that wording if appropriate. "
    "Always be helpful and accurate, but always brief."
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
                        "initial_value": clean_ai_reply(draft)
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

        property_info = get_property_info(listing_id)
        similar_examples = get_similar_learning_examples(guest_message, listing_id)
        prev_answer = ""
        if similar_examples:
            prev_answer = f"Previously, you (the host) replied to a similar guest question about this property: \"{similar_examples[0][2]}\". Use this as a guide if it fits.\n"

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
            improved = clean_ai_reply(response.choices[0].message.content.strip())
        except Exception as e:
            logging.error(f"OpenAI error in 'improve_with_ai': {e}")
            improved = "(Error generating improved reply.)"

        new_modal = view.copy()
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
        reply = clean_ai_reply(meta.get("reply", ""))
        type_ = meta.get("type")
        listing_id = meta.get("listing_id", None)
        guest_message = meta.get("guest_message", "")
        guest_id = meta.get("guest_id", None)
        ai_suggestion = reply
        # Store as learning (AI suggestion, not user-modified)
        store_learning_example(guest_message, ai_suggestion, reply, listing_id, guest_id)
        send_ok = send_reply_to_hostaway(conv_id, reply, type_)
        msg = ":white_check_mark: AI reply sent and saved!" if send_ok else ":warning: Failed to send."
        return JSONResponse({"text": msg})

    # Default: just acknowledge
    return JSONResponse({"text": "Action received."})
