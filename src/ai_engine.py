# src/ai_engine.py
import os
from typing import Dict, Any, List
import openai
from src import config

# Initialize OpenAI client
openai.api_key = config.OPENAI_API_KEY


class AIEngine:
    """Core AI engine to analyze guest conversations and generate replies."""

    def __init__(self, model: str = config.OPENAI_MODEL):
        self.model = model

    def analyze_conversation(self, conversation: str) -> Dict[str, str]:
        """
        Analyze guest conversation and extract structured insights:
        - Mood
        - Occasion
        - Summary
        """
        prompt = f"""
        You are an assistant analyzing a guest conversation at a vacation rental.
        Conversation: {conversation}

        Please extract:
        - Guest mood (happy, upset, neutral, etc.)
        - Occasion (birthday, honeymoon, business trip, unknown)
        - Summary (short 1-2 sentence recap)

        Respond in JSON:
        {{
            "mood": "...",
            "occasion": "...",
            "summary": "..."
        }}
        """

        response = openai.ChatCompletion.create(
            model=self.model,
            messages=[{"role": "system", "content": "You are a helpful assistant."},
                      {"role": "user", "content": prompt}],
            temperature=0.4,
        )

        try:
            content = response["choices"][0]["message"]["content"]
            return eval(content) if isinstance(content, str) else content
        except Exception as e:
            return {"mood": "unknown", "occasion": "unknown", "summary": "Could not analyze conversation."}

    def suggest_reply(self, guest_message: str, context: Dict[str, Any]) -> str:
        """
        Generate an AI reply suggestion based on the guest's latest message and context.
        Context can include reservation details, mood, weather, etc.
        """
        prompt = f"""
        You are an AI assistant for a vacation rental host. A guest just sent this message:

        Guest: "{guest_message}"

        Context: {context}

        Write a friendly, professional reply as the host. Keep it short and warm.
        """

        response = openai.ChatCompletion.create(
            model=self.model,
            messages=[{"role": "system", "content": "You are a professional, friendly vacation rental host assistant."},
                      {"role": "user", "content": prompt}],
            temperature=0.6,
        )

        return response["choices"][0]["message"]["content"].strip()


# --- Singleton for reuse ---
ai_engine = AIEngine()
