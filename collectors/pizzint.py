"""PizzINT API collector — primary source for pizza places and bars near Pentagon."""

import time
import httpx

from config import PIZZINT_URL, PIZZINT_TIMEOUT
from collectors.base import BaseCollector, CollectorResult, VenueData

# Bars tracked by PizzINT (detect by name)
BAR_NAMES = {"freddie's beach bar", "the little gay pub"}


def _categorize(name: str) -> str:
    return "bar" if any(b in name.lower() for b in BAR_NAMES) else "pizza"


class PizzINTCollector(BaseCollector):
    name = "pizzint"

    async def collect(self) -> CollectorResult:
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=PIZZINT_TIMEOUT) as client:
                resp = await client.get(PIZZINT_URL)
                resp.raise_for_status()
                body = resp.json()

            if not body.get("success"):
                return CollectorResult(
                    source=self.name,
                    success=False,
                    error=f"API returned success=false",
                    duration_ms=int((time.monotonic() - t0) * 1000),
                )

            venues = []
            for item in body.get("data", []):
                venues.append(
                    VenueData(
                        place_id=item["place_id"],
                        name=item["name"],
                        category=_categorize(item["name"]),
                        current_popularity=item.get("current_popularity"),
                        percentage_of_usual=item.get("percentage_of_usual"),
                        is_spike=bool(item.get("is_spike")),
                        spike_magnitude=item.get("spike_magnitude"),
                    )
                )

            return CollectorResult(
                source=self.name,
                venues=venues,
                success=True,
                duration_ms=int((time.monotonic() - t0) * 1000),
                overall_index=body.get("overall_index"),
                defcon_level=body.get("defcon_level"),
                sparkline_24h=[
                    {
                        "place_id": item["place_id"],
                        "sparkline": item.get("sparkline_24h", []),
                    }
                    for item in body.get("data", [])
                ],
            )

        except Exception as e:
            return CollectorResult(
                source=self.name,
                success=False,
                error=str(e),
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
