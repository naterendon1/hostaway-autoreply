from fastapi import APIRouter, Request, Form\
from fastapi.responses import JSONResponse\
import json\
import logging\
import requests\
import os\
\
router = APIRouter()\
\
# Ensure your API key is loaded properly\
HOSTAWAY_API_KEY = os.getenv("HOSTAWAY_API_KEY")\
HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"\
\
@router.post("/slack-interactivity")\
async def slack_action(request: Request):\
    form_data = await request.form()\
    payload = json.loads(form_data["payload"])\
    action = payload["actions"][0]\
    action_type = action["name"]\
    reservation_id = int(payload["callback_id"])\
\
    if action_type == "approve":\
        reply = action["value"]\
        send_reply_to_hostaway(reservation_id, reply)\
        return JSONResponse(\{"text": "\uc0\u9989  Reply approved and sent to guest."\})\
\
    elif action_type == "write_own":\
        return JSONResponse(\{\
            "text": "\uc0\u55357 \u56541  Please compose your message below.",\
            "attachments": [\
                \{\
                    "callback_id": str(reservation_id),\
                    "fallback": "Compose your reply",\
                    "color": "#3AA3E3",\
                    "attachment_type": "default",\
                    "actions": [\
                        \{\
                            "name": "back",\
                            "text": "\uc0\u55357 \u56601  Back",\
                            "type": "button",\
                            "value": "back"\
                        \},\
                        \{\
                            "name": "improve",\
                            "text": "\uc0\u9999 \u65039  Improve with AI",\
                            "type": "button",\
                            "value": "improve"\
                        \},\
                        \{\
                            "name": "send",\
                            "text": "\uc0\u55357 \u56552  Send",\
                            "type": "button",\
                            "value": "send"\
                        \}\
                    ]\
                \}\
            ]\
        \})\
\
    elif action_type == "back":\
        return JSONResponse(\{"text": "\uc0\u55357 \u56601  Returning to original options. (Feature coming soon)"\})\
\
    elif action_type == "improve":\
        return JSONResponse(\{"text": "\uc0\u9999 \u65039  Improve with AI feature coming soon."\})\
\
    elif action_type == "send":\
        return JSONResponse(\{"text": "\uc0\u55357 \u56552  Send functionality coming soon."\})\
\
    return JSONResponse(\{"text": "\uc0\u9888 \u65039  Unknown action"\})\
\
def send_reply_to_hostaway(message_id: int, reply_text: str):\
    # Correct the URL to match the Hostaway API documentation\
    url = f"\{HOSTAWAY_API_BASE\}/conversations/\{message_id\}/messages"\
    headers = \{\
        "Authorization": f"Bearer \{HOSTAWAY_API_KEY\}",\
        "Content-Type": "application/json"\
    \}\
    payload = \{"body": reply_text, "isIncoming": 0, "communicationType": "email"\}\
\
    logging.info(f"\uc0\u55357 \u56556  Sending reply to Hostaway (conversation ID: \{message_id\})")\
    logging.debug(f"Payload sent to Hostaway: \{json.dumps(payload, indent=2)\}")\
    \
    try:\
        r = requests.post(url, headers=headers, json=payload)\
        if r.status_code == 200:\
            logging.info(f"\uc0\u9989  Successfully sent reply to Hostaway: \{r.text\}")\
        else:\
            logging.error(f"\uc0\u10060  Hostaway reply failed: \{r.status_code\} - \{r.text\}")\
    except requests.exceptions.RequestException as e:\
        logging.error(f"\uc0\u10060  Error sending request to Hostaway: \{str(e)\}")}
