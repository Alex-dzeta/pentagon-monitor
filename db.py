"""SQLite database for storing readings history and collector health."""

from __future__ import annotations

import aiosqlite
import json
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "monitor.db"


async def init_db():
    """Create tables if they don't exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                source TEXT NOT NULL,
                place_id TEXT NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                current_popularity INTEGER,
                percentage_of_usual REAL,
                is_spike INTEGER NOT NULL DEFAULT 0,
                spike_magnitude REAL
            );

            CREATE TABLE IF NOT EXISTS index_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                overall_index REAL NOT NULL,
                defcon_level INTEGER NOT NULL,
                mode TEXT NOT NULL DEFAULT 'normal',
                venues_reporting INTEGER NOT NULL,
                venues_total INTEGER NOT NULL,
                sources_json TEXT
            );

            CREATE TABLE IF NOT EXISTS collector_health (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                collector TEXT NOT NULL,
                success INTEGER NOT NULL,
                error TEXT,
                duration_ms INTEGER
            );

            CREATE TABLE IF NOT EXISTS historical_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                defcon_peak INTEGER NOT NULL,
                index_peak REAL NOT NULL,
                event_name TEXT NOT NULL,
                description TEXT,
                duration_hours REAL,
                tags_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(timestamp);
            CREATE INDEX IF NOT EXISTS idx_index_ts ON index_history(timestamp);
            CREATE INDEX IF NOT EXISTS idx_health_ts ON collector_health(timestamp);
            CREATE INDEX IF NOT EXISTS idx_events_date ON historical_events(date);
        """)

        # Seed historical events if table is empty
        cursor = await db.execute("SELECT COUNT(*) FROM historical_events")
        count = (await cursor.fetchone())[0]
        if count == 0:
            await _seed_historical_events(db)
            await db.commit()


async def save_readings(source: str, venues: list[dict]):
    """Save venue readings snapshot."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """INSERT INTO readings
               (timestamp, source, place_id, name, category,
                current_popularity, percentage_of_usual, is_spike, spike_magnitude)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    now,
                    source,
                    v["place_id"],
                    v["name"],
                    v["category"],
                    v.get("current_popularity"),
                    v.get("percentage_of_usual"),
                    int(v.get("is_spike", False)),
                    v.get("spike_magnitude"),
                )
                for v in venues
            ],
        )
        await db.commit()


async def save_index(
    overall_index: float,
    defcon_level: int,
    mode: str,
    venues_reporting: int,
    venues_total: int,
    sources: list[str],
):
    """Save aggregated index snapshot."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO index_history
               (timestamp, overall_index, defcon_level, mode, venues_reporting, venues_total, sources_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (now, overall_index, defcon_level, mode, venues_reporting, venues_total, json.dumps(sources)),
        )
        await db.commit()


async def save_collector_health(collector: str, success: bool, error: str | None, duration_ms: int):
    """Log collector health status."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO collector_health (timestamp, collector, success, error, duration_ms) VALUES (?, ?, ?, ?, ?)",
            (now, collector, int(success), error, duration_ms),
        )
        await db.commit()


async def get_index_history(hours: int = 24) -> list[dict]:
    """Get index history for the last N hours."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT timestamp, overall_index, defcon_level, mode, venues_reporting, venues_total
               FROM index_history
               WHERE timestamp >= datetime('now', ?)
               ORDER BY timestamp ASC""",
            (f"-{hours} hours",),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_latest_readings(max_age_minutes: int = 60) -> list[dict]:
    """Get the most recent readings within max_age_minutes (for fallback cache)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT place_id, name, category, current_popularity,
                      percentage_of_usual, is_spike, spike_magnitude, source
               FROM readings
               WHERE timestamp >= datetime('now', ?)
               ORDER BY timestamp DESC""",
            (f"-{max_age_minutes} minutes",),
        )
        rows = await cursor.fetchall()
        # Deduplicate by place_id (keep most recent)
        seen = set()
        result = []
        for r in rows:
            d = dict(r)
            if d["place_id"] not in seen:
                seen.add(d["place_id"])
                result.append(d)
        return result


async def get_latest_index() -> dict | None:
    """Get the most recent index entry."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM index_history ORDER BY timestamp DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_collector_health_summary() -> list[dict]:
    """Get latest health status per collector."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT collector, success, error, duration_ms, timestamp
               FROM collector_health
               WHERE id IN (
                   SELECT MAX(id) FROM collector_health GROUP BY collector
               )"""
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_active_spikes() -> list[dict]:
    """Get venues with active spikes from the latest reading cycle."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT place_id, name, category, current_popularity,
                      percentage_of_usual, spike_magnitude, source, timestamp
               FROM readings
               WHERE is_spike = 1
                 AND timestamp >= datetime('now', '-30 minutes')
               ORDER BY spike_magnitude DESC"""
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_index_trend(hours: int = 2) -> dict:
    """Calculate index trend over the last N hours."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT overall_index, defcon_level, timestamp
               FROM index_history
               WHERE timestamp >= datetime('now', ?)
               ORDER BY timestamp ASC""",
            (f"-{hours} hours",),
        )
        rows = await cursor.fetchall()
        if not rows:
            return {"delta": 0, "direction": "stable", "period_hours": hours, "samples": 0}

        points = [dict(r) for r in rows]
        first_val = points[0]["overall_index"]
        last_val = points[-1]["overall_index"]
        delta = round(last_val - first_val, 1)

        if delta > 0.5:
            direction = "rising"
        elif delta < -0.5:
            direction = "falling"
        else:
            direction = "stable"

        # How long has it been stable/rising/falling
        stable_since = points[-1]["timestamp"]
        for i in range(len(points) - 2, -1, -1):
            prev_delta = abs(points[i]["overall_index"] - last_val)
            if prev_delta > 1.0:
                break
            stable_since = points[i]["timestamp"]

        return {
            "delta": delta,
            "direction": direction,
            "period_hours": hours,
            "samples": len(points),
            "stable_since": stable_since,
        }


async def get_sparkline(hours: int = 24, max_points: int = 48) -> list[dict]:
    """Get compact sparkline data for the widget."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT timestamp, overall_index, defcon_level
               FROM index_history
               WHERE timestamp >= datetime('now', ?)
               ORDER BY timestamp ASC""",
            (f"-{hours} hours",),
        )
        rows = await cursor.fetchall()
        points = [dict(r) for r in rows]

        # Downsample if too many points
        if len(points) > max_points:
            step = len(points) / max_points
            points = [points[int(i * step)] for i in range(max_points)]

        return [{"t": p["timestamp"], "v": p["overall_index"], "d": p["defcon_level"]} for p in points]


async def get_similar_historical_events(current_defcon: int) -> list[dict]:
    """Find historical events with similar DEFCON level."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT date, defcon_peak, index_peak, event_name, description,
                      duration_hours, tags_json
               FROM historical_events
               WHERE defcon_peak >= ?
               ORDER BY date DESC
               LIMIT 5""",
            (max(current_defcon - 1, 1),),
        )
        rows = await cursor.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d.pop("tags_json", "[]") or "[]")
            result.append(d)
        return result


async def get_all_historical_events() -> list[dict]:
    """Get all historical events."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM historical_events ORDER BY date DESC"
        )
        rows = await cursor.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d.pop("tags_json", "[]") or "[]")
            result.append(d)
        return result


async def _seed_historical_events(db):
    """Seed the historical_events table with known incidents."""
    events = [
        ("2024-01-11", 5, 9.2, "Yemen/Houthi strikes",
         "US-UK joint strikes on Houthi targets in Yemen after Red Sea attacks", 18,
         '["military", "middle_east", "oil", "shipping"]'),
        ("2024-04-13", 5, 8.7, "Iran drone attack on Israel",
         "Iran launched 300+ drones/missiles at Israel; US helped intercept", 24,
         '["military", "iran", "israel", "oil", "defense"]'),
        ("2024-10-01", 4, 7.1, "Iran missile barrage on Israel",
         "Iran fired ~180 ballistic missiles at Israel", 12,
         '["military", "iran", "israel", "oil"]'),
        ("2023-10-07", 5, 9.5, "Hamas attack on Israel",
         "Surprise Hamas attack; Pentagon activated crisis teams", 72,
         '["military", "israel", "middle_east", "oil", "gold"]'),
        ("2022-02-24", 5, 10.0, "Russia invades Ukraine",
         "Full-scale Russian invasion of Ukraine; Pentagon on highest alert", 168,
         '["military", "russia", "ukraine", "oil", "gas", "grain", "gold"]'),
        ("2020-01-03", 5, 9.8, "Soleimani assassination",
         "US killed Iranian General Soleimani via drone strike in Baghdad", 48,
         '["military", "iran", "oil", "gold", "middle_east"]'),
        ("2023-02-04", 3, 5.2, "Chinese spy balloon",
         "Chinese surveillance balloon traversed US; shot down off South Carolina", 96,
         '["china", "intelligence", "defense"]'),
        ("2024-08-01", 3, 5.8, "Haniyeh assassination",
         "Hamas leader Haniyeh killed in Tehran; regional escalation fears", 36,
         '["iran", "israel", "middle_east", "oil"]'),
        ("2023-06-24", 3, 6.1, "Wagner Group mutiny",
         "Prigozhin's march on Moscow; Pentagon monitored Russian nuclear posture", 24,
         '["russia", "military", "nuclear", "oil", "gas"]'),
        ("2022-08-02", 4, 7.5, "Pelosi Taiwan visit",
         "Speaker Pelosi visited Taiwan; China launched massive military drills", 72,
         '["china", "taiwan", "semiconductors", "defense"]'),
    ]
    await db.executemany(
        """INSERT INTO historical_events
           (date, defcon_peak, index_peak, event_name, description, duration_hours, tags_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        events,
    )
