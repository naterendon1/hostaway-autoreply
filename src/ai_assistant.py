# file: src/ai_assistant.py
"""
OpenAI Assistants API Integration for Hostaway AutoReply
--------------------------------------------------------
Uses the Assistants API to maintain conversation context across messages.
Each Hostaway conversation gets its own OpenAI thread for persistent memory.
"""

import os
import logging
import time
from typing import Dict, Any, Optional, Tuple
from openai import OpenAI

from src.db import get_thread_id, save_thread_id

# Initialize OpenAI client
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Assistant configuration
ASSISTANT_ID = None  # Will be set on initialization
ASSISTANT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Default instructions for the assistant
YOUR_VOICE_INSTRUCTIONS = """
You are ME - the property manager for a luxury rental.

YOUR PERSONALITY:
- Laid-back and easygoing
- Use casual language ("Hey there", "No worries", "You're all set")
- Short messages (1-2 sentences max)
- Friendly but not overly formal

Guidelines:
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

Common topics:
- Check-in/check-out times and procedures
- Property amenities and features
- Local recommendations (restaurants, attractions)
- House rules and policies
- WiFi passwords and access codes
- Parking information
- Emergency contacts
""".strip()


# -------------------- Assistant Management --------------------

def initialize_assistant() -> Optional[str]:
    """
    Initialize or retrieve the OpenAI Assistant.
    This should be called once on application startup.

    Returns:
        Assistant ID if successful, None otherwise
    """
    global ASSISTANT_ID

    if not client:
        logging.error("[assistant] OpenAI client not initialized - check OPENAI_API_KEY")
        return None

    try:
        # Check if we have an assistant ID stored in environment
        stored_assistant_id = os.getenv("OPENAI_ASSISTANT_ID")

        if stored_assistant_id:
            # Verify the assistant still exists
            try:
                assistant = client.beta.assistants.retrieve(stored_assistant_id)
                ASSISTANT_ID = assistant.id
                logging.info(f"[assistant] Using existing assistant: {ASSISTANT_ID}")
                return ASSISTANT_ID
            except Exception as e:
                logging.warning(f"[assistant] Stored assistant {stored_assistant_id} not found: {e}")

        # Create a new assistant
        assistant = client.beta.assistants.create(
            name="Hostaway Guest Reply Assistant",
            instructions=DEFAULT_INSTRUCTIONS,
            model=ASSISTANT_MODEL,
            tools=[],  # Can add file_search, code_interpreter if needed
        )

        ASSISTANT_ID = assistant.id
        logging.info(f"[assistant] Created new assistant: {ASSISTANT_ID}")
        logging.warning(f"[assistant] Set OPENAI_ASSISTANT_ID={ASSISTANT_ID} in environment to reuse this assistant")

        return ASSISTANT_ID

    except Exception as e:
        logging.error(f"[assistant] Failed to initialize assistant: {e}")
        return None


# -------------------- Thread Management --------------------

def get_or_create_thread(conversation_id: str) -> Optional[str]:
    """
    Get existing thread for a conversation or create a new one.

    Args:
        conversation_id: Hostaway conversation ID

    Returns:
        OpenAI thread ID if successful, None otherwise
    """
    if not client:
        return None

    # Check if we already have a thread for this conversation
    thread_id = get_thread_id(conversation_id)

    if thread_id:
        logging.info(f"[assistant] Using existing thread {thread_id} for conversation {conversation_id}")
        return thread_id

    try:
        # Create a new thread
        thread = client.beta.threads.create()
        thread_id = thread.id

        # Save the mapping
        save_thread_id(conversation_id, thread_id)

        logging.info(f"[assistant] Created new thread {thread_id} for conversation {conversation_id}")
        return thread_id

    except Exception as e:
        logging.error(f"[assistant] Failed to create thread: {e}")
        return None


# -------------------- Message Processing --------------------

def add_message_to_thread(thread_id: str, message: str, role: str = "user") -> bool:
    """
    Add a message to an existing thread.

    Args:
        thread_id: OpenAI thread ID
        message: Message content
        role: Message role ("user" or "assistant")

    Returns:
        True if successful, False otherwise
    """
    if not client:
        return False

    try:
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role=role,
            content=message
        )
        logging.debug(f"[assistant] Added {role} message to thread {thread_id}")
        return True

    except Exception as e:
        logging.error(f"[assistant] Failed to add message to thread: {e}")
        return False


def run_assistant(thread_id: str, context: Dict[str, Any]) -> Optional[str]:
    """
    Run the assistant on a thread and get the response.

    Args:
        thread_id: OpenAI thread ID
        context: Additional context (guest name, property, dates, etc.)

    Returns:
        Assistant's response text, or None if failed
    """
    if not client or not ASSISTANT_ID:
        logging.error("[assistant] Assistant not initialized")
        return None

    try:
        # Build additional instructions with context
        additional_instructions = _build_context_instructions(context)

        # Create and run
        run = client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=ASSISTANT_ID,
            additional_instructions=additional_instructions
        )

        # Wait for completion
        response_text = _wait_for_run_completion(thread_id, run.id)

        return response_text

    except Exception as e:
        logging.error(f"[assistant] Failed to run assistant: {e}")
        return None


def _build_context_instructions(context: Dict[str, Any]) -> str:
    """
    Build additional instructions from context.

    Args:
        context: Dictionary with guest/property info

    Returns:
        Formatted context string
    """
    instructions = []

    if context.get("guest_name"):
        instructions.append(f"Guest name: {context['guest_name']}")

    if context.get("check_in") and context.get("check_out"):
        instructions.append(f"Reservation: {context['check_in']} to {context['check_out']}")

    if context.get("guest_count"):
        instructions.append(f"Number of guests: {context['guest_count']}")

    if context.get("property_name"):
        instructions.append(f"Property: {context['property_name']}")

    if context.get("status"):
        instructions.append(f"Reservation status: {context['status']}")

    if instructions:
        return "Context for this conversation:\n" + "\n".join(instructions)

    return ""


def _wait_for_run_completion(thread_id: str, run_id: str, timeout: int = 30) -> Optional[str]:
    """
    Wait for an assistant run to complete and retrieve the response.

    Args:
        thread_id: OpenAI thread ID
        run_id: Run ID
        timeout: Maximum seconds to wait

    Returns:
        Assistant's response text, or None if timeout/error
    """
    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run_id)

            if run.status == "completed":
                # Get the assistant's response
                messages = client.beta.threads.messages.list(thread_id=thread_id, order="desc", limit=1)

                if messages.data:
                    message = messages.data[0]
                    if message.role == "assistant" and message.content:
                        # Extract text from content
                        for content_block in message.content:
                            if content_block.type == "text":
                                return content_block.text.value

                logging.warning("[assistant] Run completed but no assistant message found")
                return None

            elif run.status == "failed":
                logging.error(f"[assistant] Run failed: {run.last_error}")
                return None

            elif run.status == "cancelled":
                logging.warning("[assistant] Run was cancelled")
                return None

            elif run.status == "expired":
                logging.warning("[assistant] Run expired")
                return None

            # Still in progress, wait a bit
            time.sleep(0.5)

        except Exception as e:
            logging.error(f"[assistant] Error checking run status: {e}")
            return None

    logging.error(f"[assistant] Run timed out after {timeout}s")
    return None


# -------------------- High-Level Interface --------------------

def generate_reply(
    conversation_id: str,
    guest_message: str,
    context: Dict[str, Any]
) -> str:
    """
    Generate a reply to a guest message using the Assistants API.
    This maintains conversation context across messages.

    Args:
        conversation_id: Hostaway conversation ID
        guest_message: The guest's message
        context: Additional context (guest name, property, dates, etc.)

    Returns:
        AI-generated reply
    """
    # Fallback response if anything fails
    fallback = "Thanks for reaching out! We'll get back to you shortly."

    if not client or not ASSISTANT_ID:
        logging.warning("[assistant] Assistant not initialized, using fallback")
        return fallback

    try:
        # Get or create thread for this conversation
        thread_id = get_or_create_thread(conversation_id)
        if not thread_id:
            logging.error("[assistant] Failed to get/create thread")
            return fallback

        # Add the guest's message to the thread
        if not add_message_to_thread(thread_id, guest_message, role="user"):
            logging.error("[assistant] Failed to add message to thread")
            return fallback

        # Run the assistant and get response
        response = run_assistant(thread_id, context)

        if response:
            logging.info(f"[assistant] Generated reply for conversation {conversation_id}")
            return response
        else:
            logging.error("[assistant] Failed to get assistant response")
            return fallback

    except Exception as e:
        logging.error(f"[assistant] Error generating reply: {e}")
        return fallback


def analyze_conversation_thread(conversation_id: str, messages: list) -> Tuple[str, str]:
    """
    Analyze a conversation thread to determine mood and summary.
    Uses the assistant for consistency with reply generation.

    Args:
        conversation_id: Hostaway conversation ID
        messages: List of conversation messages

    Returns:
        Tuple of (mood, summary)
    """
    if not client or not ASSISTANT_ID:
        return "Neutral", "No summary available."

    try:
        # Build conversation text
        conversation_text = "\n".join(
            [f"{m.get('sender', 'Guest')}: {m.get('body', '')}" for m in messages]
        )

        # Create a temporary thread for analysis
        thread = client.beta.threads.create()
        thread_id = thread.id

        # Add analysis request
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=f"""Analyze this guest conversation and provide:
1. The guest's mood (one word: happy, confused, frustrated, excited, concerned, etc.)
2. A brief summary (one sentence)

Conversation:
{conversation_text}

Format your response as:
Mood: [mood]
Summary: [summary]"""
        )

        # Run assistant
        run = client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=ASSISTANT_ID
        )

        # Get response
        response_text = _wait_for_run_completion(thread_id, run.id)

        if response_text:
            # Parse mood and summary
            mood, summary = "Neutral", response_text
            if "Mood:" in response_text and "Summary:" in response_text:
                try:
                    mood = response_text.split("Mood:")[1].split("Summary:")[0].strip()
                    summary = response_text.split("Summary:")[1].strip()
                except Exception:
                    pass

            return mood, summary

    except Exception as e:
        logging.error(f"[assistant] Error analyzing conversation: {e}")

    return "Neutral", "No summary available."
