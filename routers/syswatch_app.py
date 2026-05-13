"""
syswatch_app.py
────────────────
Standalone entry point — run directly OR mount into STEAMI.

Run standalone:
    python syswatch_app.py

Mount into STEAMI (main.py):
    from syswatch_app import app as syswatch_app
    steami_app.mount("/syswatch", syswatch_app)

    OR just include the router:
    from routers.syswatch import router as syswatch_router
    steami_app.include_router(syswatch_router)
    steami_app.mount("/syswatch", StaticFiles(directory="static/syswatch", html=True), name="syswatch-ui")
"""

import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from routers.syswatch import router as syswatch_router

app = FastAPI(title="SysWatch", version="1.0.0")

# ── API routes ──────────────────────────────────────────────────────────────
app.include_router(syswatch_router)

# ── Static UI ───────────────────────────────────────────────────────────────
STATIC_DIR = Path(__file__).parent / "static" / "syswatch"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/ui", StaticFiles(directory=str(STATIC_DIR), html=True), name="syswatch-ui")


@app.get("/")
def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn
    print("\n  ┌─────────────────────────────────────────┐")
    print("  │  SysWatch · STEAMI Module               │")
    print("  │  http://localhost:8787                  │")
    print("  └─────────────────────────────────────────┘\n")
    uvicorn.run("syswatch_app:app", host="0.0.0.0", port=8787, reload=True)