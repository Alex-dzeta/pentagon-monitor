"""LivePopularTimes collector — fallback source for bar activity near Pentagon."""

import asyncio
import time

from config import EXTRA_BARS, LPT_RATE_LIMIT
from collectors.base import BaseCollector, CollectorResult, VenueData


class PopularTimesCollector(BaseCollector):
    name = "populartimes"

    async def collect(self) -> CollectorResult:
        t0 = time.monotonic()
        try:
            import livepopulartimes
        except ImportError:
            return CollectorResult(
                source=self.name,
                success=False,
                error="livepopulartimes not installed",
                duration_ms=int((time.monotonic() - t0) * 1000),
            )

        venues: list[VenueData] = []
        loop = asyncio.get_event_loop()

        for bar in EXTRA_BARS:
            try:
                result = await loop.run_in_executor(
                    None,
                    livepopulartimes.get_populartimes_by_address,
                    bar["address"],
                )
                if not result:
                    continue

                current_pop = result.get("current_popularity")
                venues.append(
                    VenueData(
                        place_id=result.get("place_id", bar["name"]),
                        name=bar["name"],
                        category="bar",
                        current_popularity=current_pop,
                        percentage_of_usual=None,
                        is_spike=False,
                    )
                )
                # Rate limiting
                await asyncio.sleep(1 / LPT_RATE_LIMIT)

            except Exception:
                # Skip individual venue failures
                continue

        return CollectorResult(
            source=self.name,
            venues=venues,
            success=len(venues) > 0,
            error=None if venues else "no venues returned",
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
