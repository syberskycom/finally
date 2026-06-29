# Market Data Interface

The market data subsystem (`backend/app/market/`) provides a unified API for retrieving stock prices regardless of source. The active source is selected at startup based on the `MASSIVE_API_KEY` environment variable.

---

## Architecture

```
MarketDataSource (ABC)          interface.py
├── SimulatorDataSource    ─→   simulator.py   (default, no key needed)
└── MassiveDataSource      ─→   massive_client.py  (when MASSIVE_API_KEY set)
        │
        ▼
   PriceCache (thread-safe)     cache.py
        │
        ├──→ SSE stream endpoint  (/api/stream/prices)
        ├──→ Portfolio valuation
        └──→ Trade execution
```

Both sources implement the same abstract interface. All downstream code reads from `PriceCache` — it never calls the data source directly.

---

## Data Model

### `PriceUpdate` (`models.py`)

Immutable frozen dataclass representing a single price observation:

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float
    timestamp: float          # Unix seconds

    # Computed properties
    @property
    def change(self) -> float: ...          # price - previous_price
    @property
    def change_percent(self) -> float: ...  # % change
    @property
    def direction(self) -> str: ...         # "up" | "down" | "flat"

    def to_dict(self) -> dict: ...          # JSON-serializable
```

---

## PriceCache (`cache.py`)

Thread-safe in-memory store. One writer (the active data source); many readers (SSE, portfolio, trades).

```python
class PriceCache:
    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price. Automatically computes direction from previous price.
        First update for a ticker: previous_price == price, direction == 'flat'."""

    def get(self, ticker: str) -> PriceUpdate | None:
        """Latest PriceUpdate for a ticker, or None."""

    def get_price(self, ticker: str) -> float | None:
        """Convenience: just the price float, or None."""

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices (shallow copy)."""

    def remove(self, ticker: str) -> None:
        """Remove a ticker (e.g. on watchlist removal)."""

    @property
    def version(self) -> int:
        """Monotonically increasing counter. Bumped on every update.
        SSE endpoint polls this to detect changes without copying all prices."""
```

---

## MarketDataSource Interface (`interface.py`)

Abstract base class both implementations must satisfy:

```python
class MarketDataSource(ABC):
    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing prices. Starts a background task.
        Call exactly once at app startup."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop background task and release resources. Safe to call multiple times."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present.
        Takes effect on next update cycle."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker. Also evicts it from PriceCache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Current list of tracked tickers."""
```

---

## Factory (`factory.py`)

Selects the implementation at startup:

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        return SimulatorDataSource(price_cache=price_cache)
```

| `MASSIVE_API_KEY` | Source selected |
|---|---|
| Not set or empty | `SimulatorDataSource` (GBM simulation) |
| Set and non-empty | `MassiveDataSource` (Polygon.io REST polling) |

---

## Usage

### App startup (FastAPI lifespan)

```python
from app.market import PriceCache, create_market_data_source

cache = PriceCache()
source = create_market_data_source(cache)

DEFAULT_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                   "NVDA", "META", "JPM", "V", "NFLX"]

await source.start(DEFAULT_TICKERS)
```

### Reading prices (portfolio, trade execution)

```python
update = cache.get("AAPL")         # PriceUpdate | None
price  = cache.get_price("AAPL")   # float | None
all_prices = cache.get_all()       # dict[str, PriceUpdate]
```

### Dynamic watchlist changes

```python
await source.add_ticker("PYPL")    # begins on next poll/tick
await source.remove_ticker("NFLX") # evicted from cache immediately
```

### App shutdown

```python
await source.stop()
```

### SSE streaming router

```python
from app.market import create_stream_router

router = create_stream_router(cache)
app.include_router(router)
# Mounts: GET /api/stream/prices  (text/event-stream)
```

---

## Imports

```python
# Public surface — import from the package, not submodules
from app.market import (
    PriceCache,
    PriceUpdate,
    MarketDataSource,
    create_market_data_source,
    create_stream_router,
)
```

---

## Implementation Details

### SimulatorDataSource

- Runs an asyncio task that calls `GBMSimulator.step()` every **500ms**
- Seeds `PriceCache` with initial prices before starting the loop so SSE has data immediately
- `add_ticker` seeds the cache at once so new tickers appear with a price right away
- See `MARKET_SIMULATOR.md` for the GBM math and correlation structure

### MassiveDataSource

- Runs an asyncio task that polls `GET /v2/snapshot/locale/us/markets/stocks/tickers` every **15s** (default)
- `RESTClient` is synchronous; all calls use `asyncio.to_thread()` to avoid blocking
- Does an immediate first poll in `start()` so the cache is populated before the first SSE connection
- Extracts `snap.last_trade.price` and `snap.last_trade.timestamp / 1000.0` (ms → seconds)
- On poll failure (401, 429, network error), logs and retries on the next interval — never crashes
- See `MASSIVE_API.md` for full response schema and rate limits
