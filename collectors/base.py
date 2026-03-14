"""Base collector and shared data models."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


@dataclass
class VenueData:
    place_id: str
    name: str
    category: str  # "pizza" | "bar"
    current_popularity: int | None = None  # 0-100
    percentage_of_usual: float | None = None
    is_spike: bool = False
    spike_magnitude: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CollectorResult:
    source: str  # "pizzint" | "populartimes"
    venues: list[VenueData] = field(default_factory=list)
    success: bool = False
    error: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: int = 0
    # PizzINT-specific
    overall_index: float | None = None  # 0-10
    defcon_level: int | None = None  # 1-5
    sparkline_24h: list | None = None


class BaseCollector(ABC):
    name: str = "base"

    @abstractmethod
    async def collect(self) -> CollectorResult:
        """Collect data from the source. Must not raise — return success=False on error."""
        ...

    async def health_check(self) -> bool:
        """Quick check if the source is reachable."""
        result = await self.collect()
        return result.success
