import os
import logging
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse

# Import modular routers
from src.slack_interactions import slack_interactions_bp
from src.message_handler import message_handler_bp, unified_webhook

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO)

# ---------------- FastAPI App ----------------
app = FastAPI(title="Hostaway Autoreply")

# Register routers with prefixes
app.include_router(slack_interactions_bp, prefix="/slack", tags=["slack"])
app.include_router(message_handler_bp, prefix="/webhook", tags=["webhook"])

# ---------------- Alias Route ----------------
@app.post("/unified-webhook")
async def unified_webhook_alias(request: Request):
    """
    Alias for backward compatibility — forwards to /webhook/unified-webhook.
    Hostaway posts directly here.
    """
    return await unified_webhook(request)

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
    return JSONResponse(
        {"status": "ok" if status == 200 else "missing_env", "checks": checks},
        status_code=status,
    )

@app.post("/debug-webhook")
async def debug_webhook(request: Request):
    """
    Temporary endpoint to inspect what Hostaway actually sends.
    Use it in Render logs or locally to verify JSON structure.
    """
    try:
        payload = await request.json()
        logging.info("🧩 DEBUG WEBHOOK PAYLOAD:\n%s", payload)
    except Exception as e:
        logging.error(f"Failed to parse JSON: {e}")
        payload = await request.body()
        logging.info("🧩 RAW BODY:\n%s", payload)
    
    # Return it so you can see it in browser or via curl
    return payload

# ---------------- Local Dev Runner ----------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
