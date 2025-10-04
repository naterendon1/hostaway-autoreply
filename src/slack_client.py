# src/slack_client.py
import requests
from typing import Dict, Any, List, Optional
from src import config


class SlackClient:
    """Wrapper for Slack Web API and incoming webhooks."""

    def __init__(self, bot_token: str = config.SLACK_BOT_TOKEN, webhook_url: str = config.SLACK_WEBHOOK_URL):
        self.bot_token = bot_token
        self.webhook_url = webhook_url
        self.base_url = "https://slack.com/api"

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def send_webhook(self, text: str) -> Dict[str, Any]:
        """Send a simple message using incoming webhook."""
        res = requests.post(self.webhook_url, json={"text": text})
        res.raise_for_status()
        return res.json()

    def post_message(self, channel: str, text: str, blocks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Post a message to a channel with optional Block Kit formatting."""
        url = f"{self.base_url}/chat.postMessage"
        payload = {
            "channel": channel,
            "text": text,
        }
        if blocks:
            payload["blocks"] = blocks
        res = requests.post(url, headers=self._headers(), json=payload)
        res.raise_for_status()
        return res.json()

    def open_modal(self, trigger_id: str, view: Dict[str, Any]) -> Dict[str, Any]:
        """Open a Slack modal for interactivity."""
        url = f"{self.base_url}/views.open"
        payload = {
            "trigger_id": trigger_id,
            "view": view,
        }
        res = requests.post(url, headers=self._headers(), json=payload)
        res.raise_for_status()
        return res.json()

    def update_message(self, channel: str, ts: str, text: str, blocks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Update an existing Slack message (useful for AI re-suggestions)."""
        url = f"{self.base_url}/chat.update"
        payload = {
            "channel": channel,
            "ts": ts,
            "text": text,
        }
        if blocks:
            payload["blocks"] = blocks
        res = requests.post(url, headers=self._headers(), json=payload)
        res.raise_for_status()
        return res.json()

    # --- Helpers for formatting Slack messages ---

    @staticmethod
    def build_guest_summary(guest_name: str, mood: str, occasion: str, summary: str) -> List[Dict[str, Any]]:
        """Format a guest insight summary block."""
        return [
            {"type": "header", "text": {"type": "plain_text", "text": f"Guest Insights: {guest_name}"}},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*Mood:*\n{mood}"},
                {"type": "mrkdwn", "text": f"*Occasion:*\n{occasion}"}
            ]},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Summary:*\n{summary}"}},
        ]

    @staticmethod
    def build_ai_suggestion(original_message: str, ai_reply: str) -> List[Dict[str, Any]]:
        """Format AI suggestion with buttons for quick actions."""
        return [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Guest Message:*\n{original_message}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*AI Suggestion:*\n{ai_reply}"}},
            {
                "type": "actions",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Send"}, "style": "primary", "value": "send"},
                    {"type": "button", "text": {"type": "plain_text", "text": "Edit"}, "style": "danger", "value": "edit"},
                ],
            },
        ]


# --- Singleton for reuse ---
slack_client = SlackClient()
