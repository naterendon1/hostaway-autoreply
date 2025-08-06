def get_modal_blocks(guest_name, guest_msg, draft_text, action_id="write_own", checkbox_checked=False):
    reply_block = {
        "type": "input",
        "block_id": "reply_input",
        "label": {"type": "plain_text", "text": "Your reply:" if action_id == "write_own" else "Edit below:", "emoji": True},
        "element": {
            "type": "plain_text_input",
            "action_id": "reply",
            "multiline": True,
            "initial_value": draft_text or ""
        }
    }
    checkbox_block = {
        "type": "input",
        "block_id": "save_answer_block",
        "element": {
            "type": "checkboxes",
            "action_id": "save_answer",
            "options": [{
                "text": {"type": "plain_text", "text": "Save this answer for next time", "emoji": True},
                "value": "save"
            }]
        },
        "label": {"type": "plain_text", "text": "Learning", "emoji": True},
        "optional": True
    }
    if checkbox_checked:
        checkbox_block["element"]["initial_options"] = [{
            "text": {"type": "plain_text", "text": "Save this answer for next time", "emoji": True},
            "value": "save"
        }]
    return [
        {
            "type": "section",
            "block_id": "guest_message_section",
            "text": {"type": "mrkdwn", "text": f"*Guest*: {guest_name}\n*Message*: {guest_msg}"}
        },
        reply_block,
        {
            "type": "actions",
            "block_id": "improve_ai_block",
            "elements": [
                {
                    "type": "button",
                    "action_id": "improve_with_ai",
                    "text": {"type": "plain_text", "text": "Improve with AI", "emoji": True}
                }
            ]
        },
        checkbox_block
    ]
