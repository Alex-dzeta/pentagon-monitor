"""Aggregator: merges collector results, calculates unified index, handles fallback."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import db
from config import CACHE_MAX_AGE_MINUTES, SPIKE_THRESHOLD
from collectors.base import CollectorResult, VenueData
from collectors.pizzint import PizzINTCollector
from collectors.popular_times import PopularTimesCollector

logger = logging.getLogger("aggregator")


def _defcon_from_index(index: float) -> int:
    """Calculate DEFCON level from index (0-10)."""
    if index <= 2:
        return 1
    if index <= 4:
        return 2
    if index <= 6:
        return 3
    if index <= 8:
        return 4
    return 5


async def run_collection() -> dict:
    """Run all collectors, aggregate results, save to DB. Returns current status."""

    pizzint = PizzINTCollector()
    lpt = PopularTimesCollector()

    # Run collectors in parallel
    pizzint_result, lpt_result = await asyncio.gather(
        pizzint.collect(),
        lpt.collect(),
        return_exceptions=True,
    )

    # Handle exceptions from gather
    if isinstance(pizzint_result, Exception):
        pizzint_result = CollectorResult(
            source="pizzint", success=False, error=str(pizzint_result)
        )
    if isinstance(lpt_result, Exception):
        lpt_result = CollectorResult(
            source="populartimes", success=False, error=str(lpt_result)
        )

    # Log health
    for r in (pizzint_result, lpt_result):
        await db.save_collector_health(
            collector=r.source,
            success=r.success,
            error=r.error,
            duration_ms=r.duration_ms,
        )

    # --- Determine mode and build final data ---
    mode = "normal"
    all_venues: list[VenueData] = []
    overall_index: float = 0
    defcon_level: int = 1
    sources_used: list[str] = []

    if pizzint_result.success:
        # Primary path: use PizzINT data
        all_venues.extend(pizzint_result.venues)
        overall_index = pizzint_result.overall_index or 0
        defcon_level = pizzint_result.defcon_level or _defcon_from_index(overall_index)
        sources_used.append("pizzint")

        # Optionally add extra bars from LPT (not already in PizzINT)
        if lpt_result.success:
            pizzint_ids = {v.place_id for v in pizzint_result.venues}
            for v in lpt_result.venues:
                if v.place_id not in pizzint_ids:
                    all_venues.append(v)
            sources_used.append("populartimes")

    elif lpt_result.success:
        # Degraded: PizzINT down, use LPT bars + cached pizza readings
        mode = "degraded"
        all_venues.extend(lpt_result.venues)
        sources_used.append("populartimes")

        # Load cached pizza readings
        cached = await db.get_latest_readings(max_age_minutes=CACHE_MAX_AGE_MINUTES)
        cached_pizza = [c for c in cached if c["category"] == "pizza"]
        for c in cached_pizza:
            all_venues.append(
                VenueData(
                    place_id=c["place_id"],
                    name=c["name"],
                    category="pizza",
                    current_popularity=c["current_popularity"],
                    percentage_of_usual=c["percentage_of_usual"],
                    is_spike=bool(c["is_spike"]),
                    spike_magnitude=c["spike_magnitude"],
                )
            )
        sources_used.append("cache")

        # Calculate index ourselves
        pops = [v.current_popularity for v in all_venues if v.current_popularity is not None]
        if pops:
            avg_pop = sum(pops) / len(pops)
            overall_index = round(avg_pop / 10, 1)  # normalize 0-100 → 0-10
        defcon_level = _defcon_from_index(overall_index)

    else:
        # Full offline: both sources down
        mode = "stale"
        cached = await db.get_latest_readings(max_age_minutes=CACHE_MAX_AGE_MINUTES)
        for c in cached:
            all_venues.append(
                VenueData(
                    place_id=c["place_id"],
                    name=c["name"],
                    category=c["category"],
                    current_popularity=c["current_popularity"],
                    percentage_of_usual=c["percentage_of_usual"],
                    is_spike=bool(c["is_spike"]),
                    spike_magnitude=c["spike_magnitude"],
                )
            )
        sources_used.append("cache")

        # Use last known index
        last = await db.get_latest_index()
        if last:
            overall_index = last["overall_index"]
            defcon_level = last["defcon_level"]

    # --- Save to DB ---
    venues_reporting = sum(1 for v in all_venues if v.current_popularity is not None)
    venues_total = len(all_venues)

    if all_venues:
        await db.save_readings(
            source=",".join(sources_used),
            venues=[v.to_dict() for v in all_venues],
        )

    await db.save_index(
        overall_index=overall_index,
        defcon_level=defcon_level,
        mode=mode,
        venues_reporting=venues_reporting,
        venues_total=venues_total,
        sources=sources_used,
    )

    logger.info(
        f"Collection done: mode={mode} index={overall_index} defcon={defcon_level} "
        f"venues={venues_reporting}/{venues_total} sources={sources_used}"
    )

    return {
        "index": overall_index,
        "defcon": defcon_level,
        "mode": mode,
        "venues_reporting": venues_reporting,
        "venues_total": venues_total,
        "sources": sources_used,
        "venues": [v.to_dict() for v in all_venues],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
