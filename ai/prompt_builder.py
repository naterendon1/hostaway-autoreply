# ai/prompt_builder.py

from datetime import datetime

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful, human, and context-aware vacation rental host. "
    "Reply to the guest in a friendly, concise text message, as if you were texting from your phone. "
    "Do NOT repeat what the guest just said or already confirmed—only reply with new, helpful info if needed. "
    "No greetings, no sign-offs. Always use the prior conversation (thread), reservation info, and calendar. "
    "If the guest already gave the answer, simply acknowledge or skip a reply unless clarification is needed. "
    "Don’t invent facts. Replies are sent to the guest as-is. No emojis."
)

def build_examples_section(similar_examples):
    """Format prior Q&A examples for the prompt."""
    if not similar_examples:
        return ""
    text = "\nPrevious similar guest Q&A:\n"
    for ex in similar_examples[:3]:  # limit to 3 for context size
        q = ex[0][:180].replace('\n', ' ').strip()
        a = ex[2][:180].replace('\n', ' ').strip()
        text += f"Q: {q}\nA: {a}\n"
    return text

def build_thread_section(thread_msgs):
    """Format the prior thread as dialogue, most recent last."""
    if not thread_msgs:
        return ""
    text = "\nConversation history (newest last):\n"
    for msg in thread_msgs:
        text += f"{msg}\n"
    return text

def build_reservation_section(reservation):
    """Pick only the most relevant fields for context."""
    if not reservation:
        return ""
    parts = []
    for field in ["guestFirstName", "arrivalDate", "departureDate", "numberOfGuests", "status"]:
        val = reservation.get(field)
        if val:
            parts.append(f"{field}: {val}")
    return "\nReservation: " + ', '.join(parts)

def build_listing_section(listing):
    """Show a few listing highlights (e.g., amenities, property name)."""
    if not listing:
        return ""
    result = listing.get("result", listing)  # sometimes .get("result")
    name = result.get("name", "Listing")
    amenities = result.get("amenities", [])
    if isinstance(amenities, list):
        amenities = ', '.join([str(a) for a in amenities[:5]])
    return f"\nListing: {name}\nAmenities: {amenities}"

def build_calendar_section(calendar_summary):
    if calendar_summary:
        return f"\nCalendar Info: {calendar_summary}"
    return ""

def build_intent_section(intent):
    return f"\nIntent: {intent}"

def build_full_prompt(
    guest_message, 
    thread_msgs, 
    reservation, 
    listing, 
    calendar_summary, 
    intent, 
    similar_examples,
    extra_instructions=None
):
    """
    Compose the full user prompt for OpenAI.
    """
    prompt = (
        build_examples_section(similar_examples)
        + build_thread_section(thread_msgs)
        + build_reservation_section(reservation)
        + build_listing_section(listing)
        + build_calendar_section(calendar_summary)
        + build_intent_section(intent)
        + f"\n\nGuest's latest message: \"{guest_message.strip()}\"\n"
        "---\n"
        "Write a brief, helpful reply to the most recent guest message above, using the full context. "
        "Do NOT repeat what the guest just said or already confirmed. "
        "Never add a greeting or a sign-off. Only answer the specific question, if possible."
    )
    if extra_instructions:
        prompt += f"\n{extra_instructions.strip()}"
    return prompt
