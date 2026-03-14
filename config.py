"""Configuration for Pentagon Activity Monitor."""

# --- PizzINT (primary source) ---
PIZZINT_URL = "https://www.pizzint.watch/api/dashboard-data"
PIZZINT_TIMEOUT = 10  # seconds

# --- LivePopularTimes (fallback for bars) ---
# Extra bars near Pentagon not already tracked by PizzINT
EXTRA_BARS = [
    {
        "name": "Crystal City Sports Pub",
        "address": "529 23rd St S, Arlington, VA 22202",
    },
    {
        "name": "Barley Mac",
        "address": "1600 Wilson Blvd, Arlington, VA 22209",
    },
]
LPT_RATE_LIMIT = 0.5  # max requests per second

# --- Scheduler ---
POLL_INTERVAL_MINUTES = 15

# --- Cache / fallback ---
CACHE_MAX_AGE_MINUTES = 60  # cached readings valid for 1 hour

# --- Anomaly detection ---
SPIKE_THRESHOLD = 1.5  # 150% of usual = anomaly

# --- API ---
API_KEY = None  # set via env var PENTAGON_API_KEY for POST endpoints

# --- Server ---
HOST = "0.0.0.0"
PORT = 8000
