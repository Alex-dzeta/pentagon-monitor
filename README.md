# Pentagon Activity Monitor

OSINT service that tracks pizza delivery and bar activity near the Pentagon as a geopolitical crisis indicator ("Pizza Meter").

## How it works

- Polls [PizzINT](https://pizzint.watch) API every 15 minutes for real-time venue popularity data
- Falls back to Google Popular Times (via livepopulartimes) if primary source is down
- Maintains SQLite history with trend analysis, spike detection, and historical event correlation
- Produces a unified index (0–10) and DEFCON level (1–5)

## Quick start

```bash
pip install -r requirements.txt
python main.py
```

Open http://localhost:8000 for the dashboard.

## API

All endpoints are at `/api/v1/`:

| Endpoint | Description |
|---|---|
| `GET /api/v1/index` | Current index, DEFCON, mode |
| `GET /api/v1/index/history?hours=24` | Index time series |
| `GET /api/v1/venues` | All venues with current data |
| `GET /api/v1/venues/{place_id}` | Single venue |
| `GET /api/v1/alerts` | Active spikes |
| `GET /api/v1/health` | Collector health |
| `GET /api/v1/widget` | All-in-one payload for browser extension |
| `GET /api/v1/events` | Historical events |
| `POST /api/v1/collect` | Trigger manual collection (requires API key) |

Interactive docs: http://localhost:8000/docs

## Widget endpoint

`/api/v1/widget` returns everything needed for a browser extension tooltip in a single request: index, trend, DC time context, spikes, 24h sparkline, top venues, and similar historical events.

## Architecture

```
PizzINT API (primary) ──┐
                        ├──► Aggregator ──► SQLite ──► FastAPI ──► Dashboard / API
LivePopularTimes (fallback)┘
```

Three modes: **normal** (PizzINT up), **degraded** (PizzINT down, using fallback + cache), **stale** (all sources down, cache only).
