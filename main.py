# file: main.py
import os
import logging
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, JSONResponse

# Import new modular routers
from src.slack_interactions import slack_interactions_bp
from src.message_handler import message_handler_bp

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO)

# ---------------- FastAPI App ----------------
app = FastAPI(title="Hostaway Autoreply")

# Register routers
app.include_router(slack_interactions_bp, prefix="/slack", tags=["slack"])
app.include_router(message_handler_bp, prefix="/webhook", tags=["webhook"])

# ---------------- Root Routes ----------------
@app.get("/")
async def root():
    return {"ok": True, "service": "hostaway-autoreply"}

@app.get("/ping")
async def ping():
    return PlainTextResponse("ok")

@app.get("/healthz")
async def healthz():
    """Basic environment variable checks for Render.com health probes"""
    def present(name: str) -> str:
        v = os.getenv(name)
        return "SET" if v and len(v) > 2 else "MISSING"

    checks = {
        "SLACK_BOT_TOKEN": present("SLACK_BOT_TOKEN"),
        "SLACK_CHANNEL": present("SLACK_CHANNEL"),
        "OPENAI_API_KEY": present("OPENAI_API_KEY"),
        "GOOGLE_PLACES_API_KEY": present("GOOGLE_PLACES_API_KEY"),
        "HOSTAWAY_CLIENT_ID": present("HOSTAWAY_CLIENT_ID"),
        "HOSTAWAY_CLIENT_SECRET": present("HOSTAWAY_CLIENT_SECRET"),
    }
    status = 200 if not [k for k, v in checks.items() if v == "MISSING"] else 500
    return JSONResponse({"status": "ok" if status == 200 else "missing_env", "checks": checks}, status_code=status)

# ---------------- Local Dev Runner ----------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
