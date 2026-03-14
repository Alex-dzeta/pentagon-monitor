"""Pentagon Activity Monitor — FastAPI app with scheduler and API v1."""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import db
import aggregator
from config import POLL_INTERVAL_MINUTES, API_KEY, HOST, PORT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("monitor")

# --- Latest in-memory snapshot (updated every poll) ---
_latest_status: dict | None = None

scheduler = AsyncIOScheduler()


async def scheduled_collect():
    """Scheduled task: collect data from all sources."""
    global _latest_status
    try:
        _latest_status = await aggregator.run_collection()
        logger.info(f"Scheduled collection complete: DEFCON {_latest_status['defcon']}")
    except Exception as e:
        logger.error(f"Scheduled collection failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    # Run initial collection on startup
    await scheduled_collect()
    # Schedule recurring collection
    scheduler.add_job(
        scheduled_collect,
        "interval",
        minutes=POLL_INTERVAL_MINUTES,
        id="collect",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"Scheduler started: collecting every {POLL_INTERVAL_MINUTES} min")
    yield
    scheduler.shutdown()


app = FastAPI(
    title="Pentagon Activity Monitor",
    description="OSINT pizza & bar activity index near the Pentagon",
    version="1.0.0",
    lifespan=lifespan,
)

templates = Jinja2Templates(directory="templates")

# CORS for browser extension
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# --- Auth helper for POST endpoints ---
def _check_api_key(x_api_key: Optional[str]):
    api_key = os.environ.get("PENTAGON_API_KEY", API_KEY)
    if api_key and x_api_key != api_key:
        raise HTTPException(status_code=403, detail="Invalid API key")


# ==========================================
# Dashboard (HTML)
# ==========================================

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    status = _latest_status or {}
    history = await db.get_index_history(hours=24)
    health = await db.get_collector_health_summary()
    trend = await db.get_index_trend(hours=2)
    spikes = await db.get_active_spikes()
    events = await db.get_all_historical_events()

    # DC time
    from zoneinfo import ZoneInfo
    dc_tz = ZoneInfo("America/New_York")
    dc_now = datetime.now(dc_tz)
    dc_hour = dc_now.hour
    is_weekend = dc_now.weekday() >= 5

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "status": status,
            "history": history,
            "health": health,
            "trend": trend,
            "trend_label": _trend_label(trend),
            "spikes": spikes,
            "events": events,
            "dc_time": dc_now.strftime("%H:%M"),
            "dc_context": _dc_context_label(dc_hour, is_weekend),
            "dc_is_night": dc_hour < 6 or dc_hour >= 22,
            "dc_is_weekend": is_weekend,
        },
    )


# ==========================================
# API v1 — JSON endpoints for integrations
# ==========================================

@app.get("/api/v1/index")
async def api_index():
    """Current index, DEFCON level, and mode."""
    if not _latest_status:
        raise HTTPException(status_code=503, detail="No data collected yet")
    s = _latest_status
    next_run = scheduler.get_job("collect")
    return {
        "index": s["index"],
        "defcon": s["defcon"],
        "mode": s["mode"],
        "timestamp": s["timestamp"],
        "venues_reporting": s["venues_reporting"],
        "venues_total": s["venues_total"],
        "sources": s["sources"],
        "next_update": next_run.next_run_time.isoformat() if next_run else None,
    }


@app.get("/api/v1/index/history")
async def api_index_history(hours: int = 24):
    """Index history as time series."""
    if hours < 1 or hours > 168:
        raise HTTPException(status_code=400, detail="hours must be 1-168")
    rows = await db.get_index_history(hours=hours)
    return {"hours": hours, "count": len(rows), "data": rows}


@app.get("/api/v1/venues")
async def api_venues():
    """All venues with current data."""
    if not _latest_status:
        raise HTTPException(status_code=503, detail="No data collected yet")
    return {
        "count": len(_latest_status["venues"]),
        "venues": _latest_status["venues"],
        "timestamp": _latest_status["timestamp"],
    }


@app.get("/api/v1/venues/{place_id}")
async def api_venue_by_id(place_id: str):
    """Single venue by place_id."""
    if not _latest_status:
        raise HTTPException(status_code=503, detail="No data collected yet")
    for v in _latest_status["venues"]:
        if v["place_id"] == place_id:
            return v
    raise HTTPException(status_code=404, detail="Venue not found")


@app.get("/api/v1/alerts")
async def api_alerts():
    """Active spikes and anomalies."""
    spikes = await db.get_active_spikes()
    return {
        "active_spikes": len(spikes),
        "alerts": spikes,
    }


@app.get("/api/v1/health")
async def api_health():
    """Collector health status."""
    health = await db.get_collector_health_summary()
    return {
        "collectors": health,
        "overall": "ok" if all(h["success"] for h in health) else "degraded",
    }


@app.get("/api/v1/widget")
async def api_widget():
    """Compact all-in-one payload for the browser extension tooltip.

    Returns everything the widget needs in a single request:
    - Current index + DEFCON + mode
    - Trend (delta over last 2h)
    - DC timezone context (local time, business hours flag, night flag)
    - Active spikes
    - 24h sparkline (compact)
    - Similar historical events
    - Top venues by activity
    """
    if not _latest_status:
        raise HTTPException(status_code=503, detail="No data collected yet")

    s = _latest_status

    # DC time context (Eastern Time = UTC-5 / UTC-4 DST)
    from zoneinfo import ZoneInfo
    dc_tz = ZoneInfo("America/New_York")
    dc_now = datetime.now(dc_tz)
    dc_hour = dc_now.hour
    is_business_hours = 8 <= dc_hour < 18 and dc_now.weekday() < 5
    is_night = dc_hour < 6 or dc_hour >= 22
    is_weekend = dc_now.weekday() >= 5

    # Trend
    trend = await db.get_index_trend(hours=2)

    # Sparkline
    sparkline = await db.get_sparkline(hours=24)

    # Active spikes
    spikes = await db.get_active_spikes()

    # Similar historical events
    defcon = s.get("defcon", 1)
    similar_events = await db.get_similar_historical_events(defcon)

    # Top venues by current activity
    active_venues = sorted(
        [v for v in s.get("venues", []) if v.get("current_popularity") is not None],
        key=lambda v: v["current_popularity"],
        reverse=True,
    )[:5]

    next_run = scheduler.get_job("collect")

    return {
        # Current status
        "index": s["index"],
        "defcon": defcon,
        "mode": s["mode"],
        "timestamp": s["timestamp"],
        "venues_reporting": s["venues_reporting"],
        "venues_total": s["venues_total"],
        "sources": s["sources"],
        "next_update": next_run.next_run_time.isoformat() if next_run else None,

        # Trend
        "trend": {
            "delta": trend["delta"],
            "direction": trend["direction"],
            "period_hours": trend["period_hours"],
            "label": _trend_label(trend),
        },

        # DC time context
        "dc_time": {
            "local_time": dc_now.strftime("%H:%M"),
            "timezone": str(dc_tz),
            "is_business_hours": is_business_hours,
            "is_night": is_night,
            "is_weekend": is_weekend,
            "context": _dc_context_label(dc_hour, is_weekend),
        },

        # Night activity significance
        "night_significance": (
            "HIGH — Night activity at Pentagon-area venues is a strong anomaly signal"
            if is_night and s["venues_reporting"] > 0
            else None
        ),

        # Sparkline (compact)
        "sparkline_24h": sparkline,

        # Active spikes
        "spikes": {
            "count": len(spikes),
            "venues": [
                {
                    "name": sp["name"],
                    "category": sp["category"],
                    "popularity": sp["current_popularity"],
                    "pct_of_usual": sp.get("percentage_of_usual"),
                    "magnitude": sp.get("spike_magnitude"),
                }
                for sp in spikes[:3]
            ],
        },

        # Top active venues
        "top_venues": [
            {
                "name": v["name"],
                "category": v["category"],
                "popularity": v["current_popularity"],
                "pct_of_usual": v.get("percentage_of_usual"),
            }
            for v in active_venues
        ],

        # Historical context
        "historical": {
            "similar_events": [
                {
                    "date": ev["date"],
                    "event": ev["event_name"],
                    "defcon_peak": ev["defcon_peak"],
                    "index_peak": ev["index_peak"],
                    "duration_hours": ev["duration_hours"],
                    "tags": ev["tags"],
                }
                for ev in similar_events
            ],
            "note": (
                f"Current DEFCON {defcon} — similar to {len(similar_events)} historical events"
                if similar_events
                else None
            ),
        },
    }


def _trend_label(trend: dict) -> str:
    delta = trend["delta"]
    direction = trend["direction"]
    hours = trend["period_hours"]
    if direction == "stable":
        return f"Stable for {hours}h"
    sign = "+" if delta > 0 else ""
    return f"Index {sign}{delta} over {hours}h"


def _dc_context_label(hour: int, is_weekend: bool) -> str:
    if is_weekend:
        return "Weekend — any significant activity is unusual"
    if hour < 6:
        return "Late night — activity is a strong signal"
    if hour < 8:
        return "Early morning — pre-shift activity"
    if hour < 12:
        return "Morning — normal business hours"
    if hour < 14:
        return "Lunch hours — typical peak for food venues"
    if hour < 18:
        return "Afternoon — normal business hours"
    if hour < 22:
        return "Evening — post-work activity"
    return "Late night — activity is a strong signal"


@app.get("/api/v1/events")
async def api_events():
    """All historical events."""
    events = await db.get_all_historical_events()
    return {"count": len(events), "events": events}


@app.post("/api/v1/collect")
async def api_trigger_collect(x_api_key: Optional[str] = Header(None)):
    """Manually trigger data collection."""
    _check_api_key(x_api_key)
    global _latest_status
    _latest_status = await aggregator.run_collection()
    return _latest_status


# ==========================================
# Legacy short aliases (convenience)
# ==========================================

@app.get("/api/status")
async def api_status_legacy():
    return await api_index()


@app.get("/api/health")
async def api_health_legacy():
    return await api_health()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=HOST, port=PORT, reload=True)
