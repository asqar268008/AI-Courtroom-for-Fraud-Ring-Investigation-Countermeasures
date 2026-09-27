"""FastAPI Main Application.

Run:  uvicorn app.main:app --reload
Open: http://localhost:8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import FRONTEND_DIR, settings
from app.dependencies import rt
from api.routes import (
    transactions_router,
    investigations_router,
    courtroom_router,
    countermeasures_router,
    reports_router,
    moot_court_router,
)
from api.websocket import router as websocket_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    rt.load_demo()
    rt.meta.setdefault("kind", "demo_3am")
    rt.meta.setdefault("title", "The 3 AM analyst (fixed demo)")
    yield
    await rt.llm.aclose()


app = FastAPI(title="Fraud Courtroom", version="2.0", lifespan=lifespan)

# Mount WebSockets and Sub-routers
app.include_router(websocket_router)
app.include_router(transactions_router, prefix="/api")
app.include_router(transactions_router)  # also direct /transactions/ingest
app.include_router(investigations_router, prefix="/api")
app.include_router(courtroom_router, prefix="/api")
app.include_router(courtroom_router)
app.include_router(countermeasures_router, prefix="/api")
app.include_router(countermeasures_router)  # also direct /bank/*
app.include_router(reports_router, prefix="/api")
app.include_router(moot_court_router, prefix="/api")


@app.get("/api/status")
def get_status():
    return {
        "status": "ready",
        "llm_provider": settings.llm_provider,
        "mode": settings.approval_mode,
        "llm": rt.llm.describe(),
        "stats": rt.stats,
        "active_cases": list(rt.finals.keys()),
        "meta": rt.meta,
    }


# Frontend static files and SPA command center routes
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/app.js")
    def serve_app_js():
        return FileResponse(FRONTEND_DIR / "app.js")

    @app.get("/styles.css")
    def serve_styles_css():
        return FileResponse(FRONTEND_DIR / "styles.css")

    @app.get("/")
    @app.get("/dashboard")
    @app.get("/investigations")
    @app.get("/investigations/{case_id:path}")
    @app.get("/countermeasures")
    @app.get("/moot")
    def serve_spa():
        return FileResponse(FRONTEND_DIR / "index.html")
