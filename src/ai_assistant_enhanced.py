# file: src/ai_assistant_enhanced.py
"""
Enhanced OpenAI Assistant with Deep Hostaway Integration
--------------------------------------------------------
This version pulls real data from Hostaway API and responds in your voice.
"""

import os
import logging
import time
from typing import Dict, Any, Optional, Tuple
from openai import OpenAI

from src.db import get_thread_id, save_thread_id
from src.api_client import (
    fetch_hostaway_reservation,
    fetch_hostaway_listing,
    fetch_hostaway_conversation
)

# Initialize OpenAI client
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Assistant configuration
ASSISTANT_ID = None
ASSISTANT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Your personal voice and business context
YOUR_VOICE_INSTRUCTIONS = """
You are ME - the property owner/manager. You are NOT an assistant talking to me. 
You are responding DIRECTLY to the guest as if you were the host.

You are the host’s proxy speaking directly to the guest.

You value clarity, momentum, and respect measured by usefulness rather than pleasantries.
Your default instinct is to keep conversations crisp and purpose-driven, trimming anything that doesn't move the work forward.
You're not cold—you’re economy-minded with language, and you trust guests enough not to wrap every message in padding.

Adaptive politeness:
- If the guest is warm/detailed/says “thank you”, acknowledge once (brief tokens like “Got it”, “Understood”, “You’re welcome”), then move straight to solutions. No cheese.
- If stakes are high (deadlines, compliance, urgent logistics), skip the nod and solve immediately.

Core inclination:
- Grounded directness. The most respectful thing you can offer is efficiency.
- Politeness shows up as structure, precision, responsiveness—not verbal fluff.

Acknowledgment tokens:
- Optional seasoning, not the meal. Match the guest’s tempo. Never repeat acknowledgments.

Conversational rhythm:
- Signal understanding once, then pivot fully to the task.
- Mirror the guest’s pace: fast when they’re fast, a bit more spacious when they’re verbose—always geared to action.

Underlying principle:
- “Respect through momentum.” Warm intention, concise expression, forward progress.

Hospitality expertise:
- Be an expert in hospitality, customer service, and property ops. Prioritize access, comfort, safety, and clear expectations.

Safety & privacy:
- Share door/entry codes only if the message is about access and the reservation matches.
- If there’s fire/smoke/gas/injury/flood: instruct to call the local emergency number immediately (Germany/most EU: 112; US/CA: 911), evacuate if relevant, then ask them to confirm safety.
- Don’t disclose internal emails, tokens, or non-guest-facing numbers.
- Never mention you’re an AI; you are the host’s representative.

YOUR PERSONALITY & TONE:
- Friendly and warm, but not overly formal
- Concise - get to the point quickly (2-3 sentences usually)
- Informal and conversational (use contractions: you'll, we're, that's, etc.)
- Helpful and proactive
- Professional but not corporate

COMMUNICATION STYLE:
- Write like you're texting a friend, not writing a business email
- No emojis unless the guest uses them first
- No formal sign-offs like "Sincerely" or "Best regards"
- Just end naturally after answering the question
- Use "I" not "we" when it makes sense

HOW I RUN MY BUSINESS:
- I care about guests having a great experience
- I'm responsive and helpful
- I give clear, specific instructions
- I confirm important details explicitly
- I proactively offer relevant info without being asked

WHAT TO DO:
✓ Answer questions directly using REAL data from Hostaway
✓ Reference specific details (check-in time, address, amenities)
✓ Be proactive (if they ask about check-in, also mention parking)
✓ Keep it brief - guests are busy
✓ Sound human and natural

WHAT NOT TO DO:
✗ Don't use placeholders like [insert time] or {property name}
✗ Don't be overly formal or corporate
✗ Don't make up information you don't have
✗ Don't use emojis excessively
✗ Don't write long paragraphs
✗ Don't say "As an AI" or reference being an assistant

- NEVER use sign-offs: No "Best", "Sincerely", "Best regards", "Cheers", "[Your Name]", etc.
- Just end naturally after answering the question - like a text message

ENDING MESSAGES:
✓ GOOD: "San Gabriel Park is about 2.5 miles away - 5-10 minute drive."
✗ BAD: "Best, [Your Name]" or any formal closing
Just STOP after the helpful information. Think text message, not email.

HANDLING UNKNOWNS:
If you don't have specific information, say something like:
- "Let me double-check that for you and get back to you shortly"
- "I'll confirm that and send you the details"
- NOT: "I don't have that information" (too cold)
- NOT: "[Insert details here]" (sounds robotic)

Remember: You ARE the host. The guest is talking to YOU, not an AI assistant.
""".strip()


# -------------------- Assistant Management --------------------

def initialize_enhanced_assistant() -> Optional[str]:
    """
    Initialize or retrieve the enhanced OpenAI Assistant with your voice.
    """
    global ASSISTANT_ID

    if not client:
        logging.error("[assistant] OpenAI client not initialized - check OPENAI_API_KEY")
        return None

    try:
        # Check if we have an assistant ID stored
        stored_assistant_id = os.getenv("OPENAI_ASSISTANT_ID")

        if stored_assistant_id:
            try:
                assistant = client.beta.assistants.retrieve(stored_assistant_id)
                ASSISTANT_ID = assistant.id
                logging.info(f"[assistant] Using existing assistant: {ASSISTANT_ID}")
                return ASSISTANT_ID
            except Exception as e:
                logging.warning(f"[assistant] Stored assistant not found: {e}")

        # Create new assistant with enhanced instructions
        assistant = client.beta.assistants.create(
            name="Hostaway Smart Reply (Your Voice)",
            instructions=YOUR_VOICE_INSTRUCTIONS,
            model=ASSISTANT_MODEL,
            tools=[],
        )

        ASSISTANT_ID = assistant.id
        logging.info(f"[assistant] Created new assistant: {ASSISTANT_ID}")
        logging.warning(f"[assistant] Set OPENAI_ASSISTANT_ID={ASSISTANT_ID} in environment")

        return ASSISTANT_ID

    except Exception as e:
        logging.error(f"[assistant] Failed to initialize assistant: {e}")
        return None


# -------------------- Context Building --------------------

def build_rich_context(context: Dict[str, Any]) -> str:
    """
    Build comprehensive context from Hostaway data.
    This fetches real listing and reservation details.
    """
    parts = []
    
    # === RESERVATION DETAILS ===
    reservation_id = context.get("reservation_id")
    if reservation_id:
        try:
            res_data = fetch_hostaway_reservation(reservation_id)
            if res_data and res_data.get("result"):
                r = res_data["result"]
                
                parts.append("=== CURRENT RESERVATION ===")
                parts.append(f"Guest: {r.get('guestFirstName', '')} {r.get('guestLastName', '')}")
                parts.append(f"Email: {r.get('guestEmail', '')}")
                parts.append(f"Check-in: {r.get('arrivalDate', '')}")
                parts.append(f"Check-out: {r.get('departureDate', '')}")
                parts.append(f"Guests: {r.get('numberOfGuests', 'N/A')}")
                parts.append(f"Status: {r.get('status', 'N/A')}")
                parts.append(f"Total Price: {r.get('currency', '')} {r.get('totalPrice', 'N/A')}")
                
                # Important notes
                if r.get('guestNote'):
                    parts.append(f"Guest Note: {r['guestNote']}")
                if r.get('doorCode'):
                    parts.append(f"Door Code: {r['doorCode']}")
                if r.get('phone'):
                    parts.append(f"Phone: {r['phone']}")
                
                # Store listing_id for next section
                context['listing_id'] = r.get('listingMapId')
        except Exception as e:
            logging.error(f"Error fetching reservation: {e}")
    
    # === LISTING/PROPERTY DETAILS ===
    listing_id = context.get("listing_id")
    if listing_id:
        try:
            listing_data = fetch_hostaway_listing(listing_id)
            if listing_data and listing_data.get("result"):
                prop = listing_data["result"]
                
                parts.append("\n=== PROPERTY DETAILS ===")
                parts.append(f"Property: {prop.get('name', 'N/A')}")
                parts.append(f"Address: {prop.get('address', 'N/A')}")
                parts.append(f"City: {prop.get('city', 'N/A')}, {prop.get('state', 'N/A')}")
                
                # Key property features
                parts.append(f"Bedrooms: {prop.get('bedroomsNumber', 'N/A')}")
                parts.append(f"Beds: {prop.get('bedsNumber', 'N/A')}")
                parts.append(f"Bathrooms: {prop.get('bathroomsNumber', 'N/A')}")
                parts.append(f"Max Guests: {prop.get('personCapacity', 'N/A')}")
                parts.append(f"Room Type: {prop.get('roomType', 'N/A')}")
                
                # Check-in/out times
                check_in_start = prop.get('checkInTimeStart')
                check_in_end = prop.get('checkInTimeEnd')
                check_out = prop.get('checkOutTime')
                
                if check_in_start is not None:
                    parts.append(f"Check-in: {check_in_start}:00 - {check_in_end}:00" if check_in_end else f"Check-in: After {check_in_start}:00")
                if check_out is not None:
                    parts.append(f"Check-out: {check_out}:00")
                
                # WiFi
                if prop.get('wifiUsername') or prop.get('wifiPassword'):
                    wifi_info = []
                    if prop.get('wifiUsername'):
                        wifi_info.append(f"Network: {prop['wifiUsername']}")
                    if prop.get('wifiPassword'):
                        wifi_info.append(f"Password: {prop['wifiPassword']}")
                    parts.append(f"WiFi: {' | '.join(wifi_info)}")
                
                # Special instructions
                if prop.get('specialInstruction'):
                    parts.append(f"Special Instructions: {prop['specialInstruction']}")
                if prop.get('keyPickup'):
                    parts.append(f"Key Pickup: {prop['keyPickup']}")
                if prop.get('doorSecurityCode'):
                    parts.append(f"Door Code: {prop['doorSecurityCode']}")
                
                # House rules
                if prop.get('houseRules'):
                    parts.append(f"House Rules: {prop['houseRules']}")
                
                # Amenities (if available)
                if prop.get('listingAmenities'):
                    amenity_count = len(prop['listingAmenities'])
                    parts.append(f"Amenities: {amenity_count} available")
                    # You can fetch full amenity names via /v1/amenities if needed
                
        except Exception as e:
            logging.error(f"Error fetching listing: {e}")
    
    # === CONVERSATION CONTEXT ===
    conversation_id = context.get("conversation_id")
    if conversation_id:
        try:
            conv_data = fetch_hostaway_conversation(conversation_id)
            if conv_data and conv_data.get("result"):
                conv = conv_data["result"]
                
                # Get recent messages for context
                messages = conv.get("conversationMessages", [])
                if messages:
                    parts.append("\n=== RECENT CONVERSATION ===")
                    # Show last 5 messages
                    for msg in messages[-5:]:
                        sender = "Guest" if msg.get("isIncoming") else "You (Host)"
                        body = msg.get("body", "")[:200]  # Limit length
                        parts.append(f"{sender}: {body}")
        except Exception as e:
            logging.error(f"Error fetching conversation: {e}")
    
    return "\n".join(parts) if parts else ""


# -------------------- Reply Generation --------------------

def generate_smart_reply(
    conversation_id: str,
    guest_message: str,
    context: Dict[str, Any]
) -> str:
    """
    Generate a reply using the enhanced assistant with full Hostaway context.
    
    Args:
        conversation_id: Hostaway conversation ID
        guest_message: The guest's latest message
        context: Dict with reservation_id, listing_id, conversation_id, etc.
    
    Returns:
        AI-generated reply in your voice
    """
    fallback = "Thanks for reaching out! Let me look into that and get back to you shortly."

    if not client or not ASSISTANT_ID:
        logging.warning("[assistant] Assistant not initialized")
        return fallback

    try:
        # Get or create thread
        thread_id = get_or_create_thread(conversation_id)
        if not thread_id:
            logging.error("[assistant] Failed to get/create thread")
            return fallback

        # Build rich context from Hostaway API
        rich_context = build_rich_context(context)
        
        # Combine guest message with context
        full_message = f"""{rich_context}

=== GUEST'S NEW MESSAGE ===
{guest_message}

---
Remember: You are the HOST responding to this guest. Use the REAL information above.
Keep it friendly, brief, and natural. No placeholders - use actual details from above."""

        # Add message to thread
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=full_message
        )

        # Run assistant
        run = client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=ASSISTANT_ID
        )

        # Wait for completion
        response = _wait_for_run_completion(thread_id, run.id)

        if response:
            logging.info(f"[assistant] Generated reply for conversation {conversation_id}")
            return response
        else:
            logging.error("[assistant] Failed to get response")
            return fallback

    except Exception as e:
        logging.error(f"[assistant] Error generating reply: {e}")
        return fallback


# -------------------- Helper Functions --------------------

def get_or_create_thread(conversation_id: str) -> Optional[str]:
    """Get or create OpenAI thread for conversation."""
    if not client:
        return None

    thread_id = get_thread_id(conversation_id)
    if thread_id:
        logging.info(f"[assistant] Using thread {thread_id}")
        return thread_id

    try:
        thread = client.beta.threads.create()
        thread_id = thread.id
        save_thread_id(conversation_id, thread_id)
        logging.info(f"[assistant] Created thread {thread_id}")
        return thread_id
    except Exception as e:
        logging.error(f"[assistant] Failed to create thread: {e}")
        return None


def _wait_for_run_completion(thread_id: str, run_id: str, timeout: int = 30) -> Optional[str]:
    """Wait for assistant run to complete and return response."""
    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run_id)

            if run.status == "completed":
                messages = client.beta.threads.messages.list(
                    thread_id=thread_id,
                    order="desc",
                    limit=1
                )

                if messages.data:
                    message = messages.data[0]
                    if message.role == "assistant" and message.content:
                        for content_block in message.content:
                            if content_block.type == "text":
                                return content_block.text.value

                return None

            elif run.status in ["failed", "cancelled", "expired"]:
                logging.error(f"[assistant] Run {run.status}: {getattr(run, 'last_error', 'N/A')}")
                return None

            time.sleep(0.5)

        except Exception as e:
            logging.error(f"[assistant] Error checking run: {e}")
            return None

    logging.error(f"[assistant] Run timed out after {timeout}s")
    return None
