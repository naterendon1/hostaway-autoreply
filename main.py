# file: main.py
"""
Main entrypoint for Hostaway AutoReply Service
----------------------------------------------
Handles:
- Slack interactive actions (/slack routes)
- Hostaway webhook events (/webhook routes)
- Health checks for Render deployment
"""

import os
import logging
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, JSONResponse

# Import routers
from src.slack_interactions import slack_interactions_bp
from src.message_handler import message_handler_bp

# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hostaway-autoreply")

# ---------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------
app = FastAPI(title="Hostaway Autoreply Service")

# Register Routers
app.include_router(slack_interactions_bp, prefix="/slack", tags=["slack"])
app.include_router(message_handler_bp, prefix="/webhook", tags=["webhook"])


# ---------------------------------------------------------------------
# Root Routes
# ---------------------------------------------------------------------
@app.get("/")
async def root():
    """Basic root endpoint for quick verification."""
    return {"ok": True, "service": "hostaway-autoreply"}


@app.get("/ping")
async def ping():
    """Lightweight endpoint for uptime pings."""
    return PlainTextResponse("ok")


@app.get("/healthz")
async def healthz():
    """Health check for Render.com / Kubernetes probes."""
    def present(name: str) -> str:
        val = os.getenv(name)
        return "SET" if val and len(val) > 2 else "MISSING"

    checks = {
        "SLACK_BOT_TOKEN": present("SLACK_BOT_TOKEN"),
        "SLACK_CHANNEL": present("SLACK_CHANNEL"),
        "OPENAI_API_KEY": present("OPENAI_API_KEY"),
        "HOSTAWAY_ACCESS_TOKEN": present("HOSTAWAY_ACCESS_TOKEN"),
        "HOSTAWAY_CLIENT_ID": present("HOSTAWAY_CLIENT_ID"),
        "HOSTAWAY_CLIENT_SECRET": present("HOSTAWAY_CLIENT_SECRET"),
    }

    missing = [k for k, v in checks.items() if v == "MISSING"]
    status_code = 200 if not missing else 500

    return JSONResponse(
        {"status": "ok" if status_code == 200 else "missing_env", "checks": checks},
        status_code=status_code,
    )


# ---------------------------------------------------------------------
# Local Dev Runner
# ---------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "5000"))
    logger.info(f"Starting Hostaway Autoreply Service on port {port}")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
