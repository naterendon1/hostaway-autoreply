Hostaway AutoReply

hostaway-autoreply is a Python-based app that automates replying to guest messages in Hostaway (or a Hostaway-style messaging API) using heuristics / logic. It routes messages through an “assistant core,” memory, and template logic to produce intelligent responses.

This README covers:

Overview / purpose

Features

Architecture & file structure

Requirements / dependencies

Setup & configuration

Running / usage

How it works (flow)

Extending / customizing

Logging, error handling & retries

Deployment / production notes

Security & secrets

Contributing

License

1. Overview / Purpose

The goal of hostaway-autoreply is to automatically generate responses to inbound guest messages (e.g. via Hostaway inbox or similar) using programmable logic and templates, while maintaining memory/context across conversations.

Key benefits:

Reduce manual effort by automatically replying to common queries

Maintain consistency in tone/style

Use past memory/context per guest or conversation

Be extensible/customizable for new rules or logic

It is not a fully autonomous AI — you should review or monitor replies if desired; the logic is deterministic and transparent.

2. Features

Message ingestion / parsing

Conversation memory / context storage

Template-based reply generation

Rule-based logic or conditional branching

Integration points for external services (e.g. Hostaway’s API, Slack, etc.)

Logging, metrics, error handling

Configuration via YAML / JSON for template definitions

Modular code structure (core, utils, config, etc.)

3. Architecture & File Structure

Below is a high-level view of the core files and modules (as seen in the repository):

.
├── assistant_core.py
├── main.py
├── db.py
├── utils.py
├── slack_interactivity.py
├── places.py
├── memory.yaml
├── render.yaml
├── config/        ← configuration directory (if present)
│   └── …
├── github/        ← GitHub/workflows (CI)  
├── utils/         ← supplementary utilities  
│   └── …
├── requirements.txt
└── .gitignore


Descriptions:

main.py — Entry point. Handles receiving inbound message events, routing to core logic, sending replies.

assistant_core.py — The “brain” of the reply system: logic to select templates, apply memory/context, generate reply text.

db.py — Persistence / storage module. Could be file-based or database — stores memory, conversation state, etc.

utils.py — Helper functions (string utility, sanitization, formatting, etc.).

slack_interactivity.py — Logic for Slack-based operations (if the app supports Slack triggers or alerts).

places.py — Possibly a module dealing with location-based logic or “place” templates (e.g. local recommendations).

memory.yaml — YAML file storing memory/context state (if using a file-based memory store).

render.yaml — Configuration of templates/rules for rendering replies.

config/ — Additional configuration files (e.g. secrets, environment-specific settings).

github/ workflows — CI/CD workflows (tests, linting, deployment).

utils/ — More specialized helper utilities, submodules.

requirements.txt — Python dependencies.

4. Requirements / Dependencies

Inspect requirements.txt to see specific versions. Typical dependencies might include:

requests or httpx — for HTTP API calls

PyYAML — for YAML config parsing

flask / fastapi / any web framework (if hosting HTTP endpoints)

slack-sdk or similar (if Slack integration)

pytest / unittest for tests

Logging / monitoring libs

Make sure to use a virtual environment (venv, pipenv, poetry) to isolate dependencies.

5. Setup & Configuration
5.1 Clone repository
git clone https://github.com/naterendon1/hostaway-autoreply.git
cd hostaway-autoreply

5.2 Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate    # Unix / macOS
# or
venv\Scripts\activate       # Windows


Install dependencies:

pip install -r requirements.txt

5.3 Configuration files & secrets

You’ll need to supply configuration for:

Hostaway API credentials (API key, endpoint URL)

Slack (if used): webhook URL, bot token, etc.

Template files / YAML configs in render.yaml

Memory storage setup (file path, DB connection)

Environment-specific settings (e.g. in a .env file: HOSTAWAY_API_KEY, SLACK_TOKEN, etc.)

Ensure secrets are never committed to Git. Use environment variables or a secrets manager.

5.4 Initialize memory (if needed)

If using memory.yaml or another file-based memory, ensure the file exists and is in correct format (e.g. an empty YAML structure).

6. Running / Usage

You can run in development mode:

python main.py


If main.py sets up a server or listener, you may need to specify:

python main.py --port 8000


Or use environment variables, e.g.:

export PORT=8000
python main.py


When a guest message is received (via webhook or polling), main.py dispatches it to assistant_core, which returns a reply. Then your app should send the reply via Hostaway’s messaging API.

If Slack interactivity is enabled, commands from Slack (e.g. approve, reject, review) may trigger functions in slack_interactivity.py.

7. How It Works (Flow)

Here is a simplified flow:

Incoming message event
The app receives a webhook or poll result representing a guest’s message (with fields: guest id, conversation id, message text, timestamp, etc.).

Load memory / context
Look up previous messages, guest data, conversation context in db.py or memory.yaml.

Core logic / assistant processing
In assistant_core.py:

Classify the message (e.g. booking inquiry, check-in question, amenity question, local recommendation)

Match appropriate template(s) from render.yaml

Fill template variables (guest name, dates, listing info, local places, etc.)

Possibly craft fallback responses if no rule matches

Persist updated memory/context
Update conversation history, mark message as replied, store new memory data.

Send reply
Use Hostaway API (or the messaging API) to post the reply. Optionally, queue it for manual review or pass it directly.

(Optional) Slack / admin notification
If configured, send summary to Slack or allow human override / audit.

Error / retry handling
If API call fails, implement retries, backoff, logging.

8. Extending / Customizing
8.1 Adding templates / rules

Edit render.yaml (or similar config) to add new template types or message patterns

Use placeholders / variables (e.g. {{ guest_name }}, {{ checkin_date }})

Add conditions: only apply if message contains certain keywords or matches regex

8.2 Changing memory backend

If the current memory is file-based (memory.yaml), you may want to switch to a database (SQLite, PostgreSQL). You’ll need to update db.py:

Provide get_conversation_state(conversation_id)

update_memory(…)

Migrations, schema

8.3 Plug-in modules

You can write modules to handle:

Local recommendations (e.g. in places.py)

API integrations (weather, maps, events)

Advanced NLP / ML models (to classify or generate responses)

Make sure to follow the structure: separation of core logic vs side effects.

9. Logging, Error Handling & Retries

Use Python’s logging library to emit logs at different levels (DEBUG, INFO, WARNING, ERROR).

Log inbound and outbound messages, template decisions, errors.

Wrap external API calls (Hostaway, Slack) with retries and exponential backoff.

In production, consider integrating with monitoring (Sentry, DataDog).

10. Deployment & Production Notes

Containerization: Write a Dockerfile so the app can run in a Docker container.

Environment Variables: Use Docker secrets or .env for configuration

Scaling: If many messages per second, consider concurrency, async I/O

Security: Use TLS / HTTPS endpoints. Validate incoming webhooks (secret tokens).

Rate limiting: Respect Hostaway API rate limits

Backup memory / database regularly

Health check / liveness endpoints

11. Security & Secrets

Never store API keys or secrets in version control

Use environment variables or a secrets manager

Restrict network permissions (only access outbound APIs)

Validate incoming requests (e.g. HMAC signature, IP whitelist)

Sanitize inputs (message text) to avoid injection

12. Contributing & Development Workflow

Use feature branches (e.g. feature/auto-reply-rule)

Write tests covering core logic (assistant_core, template filling, matching)

Use CI (GitHub Actions) to run linting, tests on pull requests

Document any new behavior or config changes
