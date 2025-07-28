import logging
from db import save_custom_response, save_learning_example, save_ai_feedback

# Logs thumbs up/down feedback from user

def store_ai_feedback(conv_id, question, answer, rating, user):
    logging.info(f"📥 Feedback: [{rating.upper()}] by {user} on conv {conv_id}")
    save_ai_feedback(conv_id, question, answer, rating, user)

# Stores rewritten AI reply from Edit modal

def store_learning_example(listing_id, guest_question, corrected_reply):
    logging.info("💡 Storing learning example from EDIT")
    save_learning_example(listing_id, guest_question, corrected_reply)

# Stores full custom user-written response

def store_custom_response(listing_id, guest_question, custom_reply):
    logging.info("📝 Storing custom response from WRITE YOUR OWN")
    save_custom_response(listing_id, guest_question, custom_reply)

# Alert sent to admin when user writes their own reply (optional)

def notify_admin_of_custom_response(metadata, reply):
    logging.info("📣 Admin notified of custom reply")
    # Placeholder: could trigger Slack or email notification
    return
