# Market Data Backend — Design

**Status:** Implemented in `backend/app/market/`. This document is the detailed design reference: the unified interface, the GBM simulator, the Massive (Polygon.io) client, the shared price cache, and the SSE streaming endpoint, with the actual code.

For a one-page overview see `planning/MARKET_DATA_SUMMARY.md`.

---

## 1. Goals

- One abstract interface (`MarketDataSource`) with two interchangeable implementations: a built-in GBM simulator (default, zero config) and a Massive/Polygon.io REST poller (used when `MASSIVE_API_KEY` is set).
- A single thread-safe in-memory `PriceCache` that producers write to and all consumers (SSE stream, portfolio valuation, trade execution) read from. Downstream code never talks to a data source directly.
- An SSE endpoint (`GET /api/stream/prices`) that pushes the full price set to connected clients whenever anything changes, at ~500ms cadence.
- Dynamic watchlist support — tickers can be added/removed at runtime without restarting the source.

```
MarketDataSource (ABC)
├── SimulatorDataSource  →  GBMSimulator (correlated geometric Brownian motion)
└── MassiveDataSource    →  Polygon.io REST poller (massive package)
        │
        ▼
   PriceCache (thread-safe, in-memory, version-counted)
        │
        ├──→ SSE stream endpoint (/api/stream/prices)
        ├──→ Portfolio valuation
        └──→ Trade execution
```

Module layout (`backend/app/market/`):

| File | Purpose |
|---|---|
| `models.py` | `PriceUpdate` — immutable price snapshot |
| `interface.py` | `MarketDataSource` ABC |
| `cache.py` | `PriceCache` — thread-safe store with a version counter |
| `seed_prices.py` | Seed prices, per-ticker GBM params, correlation groups |
| `simulator.py` | `GBMSimulator` + `SimulatorDataSource` |
| `massive_client.py` | `MassiveDataSource` |
| `factory.py` | `create_market_data_source()` |
| `stream.py` | `create_stream_router()` (SSE) |
| `__init__.py` | Public exports |

---

## 2. Data Model — `PriceUpdate`

An immutable, frozen dataclass representing one ticker's price at a point in time. `change`, `change_percent`, and `direction` are derived properties so producers only need to supply `ticker`, `price`, `previous_price`.

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker, "price": self.price,
            "previous_price": self.previous_price, "timestamp": self.timestamp,
            "change": self.change, "change_percent": self.change_percent,
            "direction": self.direction,
        }
```

`slots=True` + `frozen=True` keeps this cheap to allocate on every tick (10 tickers × 2 Hz) and prevents accidental mutation across threads.

---

## 3. Unified Interface — `MarketDataSource`

```python
class MarketDataSource(ABC):
    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing updates. Starts a background asyncio task. Call once."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task. Safe to call multiple times."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker. Also evicts it from the PriceCache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Currently tracked tickers."""
```

Both implementations satisfy this contract identically from the caller's point of view — `app/main.py` (or wherever the app wires startup) only ever sees `MarketDataSource`, never the concrete class. This is a Strategy pattern: swapping data sources is an environment-variable decision, not a code change.

---

## 4. Shared Price Cache — `PriceCache`

Single point of truth. Producers (`SimulatorDataSource` or `MassiveDataSource` — never both at once) write; consumers read. A monotonic `version` counter lets the SSE endpoint cheaply detect "did anything change since I last looked" without diffing dictionaries.

```python
class PriceCache:
    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        with self._lock:
            ts = timestamp or time.time()
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price  # first update: flat
            update = PriceUpdate(
                ticker=ticker, price=round(price, 2),
                previous_price=round(previous_price, 2), timestamp=ts,
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None: ...
    def get_all(self) -> dict[str, PriceUpdate]: ...   # shallow copy, lock-protected
    def get_price(self, ticker: str) -> float | None: ...
    def remove(self, ticker: str) -> None: ...

    @property
    def version(self) -> int: ...
```

A plain `threading.Lock` (not an asyncio lock) is correct here: `PriceCache.update()` is called both from the simulator's asyncio loop and potentially from a thread (`asyncio.to_thread` in the Massive client), and reads happen from FastAPI's event loop. The critical sections are tiny (dict get/set), so lock contention is a non-issue at this scale.

---

## 5. Simulator — `GBMSimulator` / `SimulatorDataSource`

### 5.1 Math

Geometric Brownian Motion, the standard model for simulating a stock price path:

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

- `mu` — annualized drift (expected return)
- `sigma` — annualized volatility
- `dt` — time step as a fraction of a trading year
- `Z` — standard normal random draw (correlated across tickers, see below)

The tick interval is 500ms; expressed as a fraction of a 252-day, 6.5h/day trading year that's `dt ≈ 8.48e-8`, which produces realistic sub-cent moves per tick that compound into believable intraday paths over a session.

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.48e-8
```

### 5.2 Correlated moves via Cholesky decomposition

Real markets don't move ticker-by-ticker independently — tech stocks tend to move together, financials move together, etc. The simulator builds a correlation matrix from sector groupings and uses its Cholesky factor `L` (where `L @ L.T == corr`) to turn independent normal draws into correlated ones: `z_correlated = L @ z_independent`.

```python
CORRELATION_GROUPS = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}
INTRA_TECH_CORR = 0.6
INTRA_FINANCE_CORR = 0.5
CROSS_GROUP_CORR = 0.3
TSLA_CORR = 0.3   # TSLA correlates weakly with everything — "does its own thing"

@staticmethod
def _pairwise_correlation(t1: str, t2: str) -> float:
    if t1 == "TSLA" or t2 == "TSLA":
        return TSLA_CORR
    if t1 in tech and t2 in tech:
        return INTRA_TECH_CORR
    if t1 in finance and t2 in finance:
        return INTRA_FINANCE_CORR
    return CROSS_GROUP_CORR
```

The matrix (and its Cholesky factor) is rebuilt whenever a ticker is added or removed — `O(n^2)` to build, `O(n^3)` to factor, both negligible since `n` stays well under 50.

### 5.3 The hot path — `step()`

Called every 500ms for every active ticker. Kept allocation-light: one `numpy` draw per tick, then a tight per-ticker loop.

```python
def step(self) -> dict[str, float]:
    n = len(self._tickers)
    if n == 0:
        return {}

    z_independent = np.random.standard_normal(n)
    z_correlated = self._cholesky @ z_independent if self._cholesky is not None else z_independent

    result: dict[str, float] = {}
    for i, ticker in enumerate(self._tickers):
        mu, sigma = self._params[ticker]["mu"], self._params[ticker]["sigma"]
        drift = (mu - 0.5 * sigma**2) * self._dt
        diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
        self._prices[ticker] *= math.exp(drift + diffusion)

        # ~0.1% chance per tick of a 2-5% shock, for visual drama
        if random.random() < self._event_prob:
            shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
            self._prices[ticker] *= 1 + shock

        result[ticker] = round(self._prices[ticker], 2)
    return result
```

With 10 tickers at 2 ticks/sec and `event_probability=0.001`, a random shock fires roughly once every 50 seconds across the whole watchlist — frequent enough to be visible in the UI, rare enough not to dominate.

### 5.4 Seed data (`seed_prices.py`)

Realistic starting prices and per-ticker `(mu, sigma)` for the 10 default tickers, plus a `DEFAULT_PARAMS` fallback (`sigma=0.25, mu=0.05`) for tickers added dynamically that aren't in the curated list (e.g. a user adds `PYPL` via chat):

```python
SEED_PRICES = {"AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, ...}
TICKER_PARAMS = {
    "TSLA": {"sigma": 0.50, "mu": 0.03},   # high vol
    "NVDA": {"sigma": 0.40, "mu": 0.08},   # high vol, strong drift
    "JPM":  {"sigma": 0.18, "mu": 0.04},   # low vol (bank)
    ...
}
DEFAULT_PARAMS = {"sigma": 0.25, "mu": 0.05}
```

### 5.5 `SimulatorDataSource` — the asyncio wrapper

Owns a background `asyncio.Task` that ticks the simulator and writes results into the shared cache. Seeds the cache synchronously on `start()` / `add_ticker()` so the UI never shows an empty cell waiting for the first tick.

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5,
                 event_probability: float = 0.001) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        for ticker in tickers:
            self._cache.update(ticker=ticker, price=self._sim.get_price(ticker))
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            self._cache.update(ticker=ticker, price=self._sim.get_price(ticker))

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

    async def _run_loop(self) -> None:
        while True:
            try:
                if self._sim:
                    for ticker, price in self._sim.step().items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

The `try/except Exception` around the step + cache write means one bad tick (e.g. a numpy edge case) logs and skips rather than killing the background task — the loop is the only producer, so it must never die silently.

---

## 6. Real Data — `MassiveDataSource` (Polygon.io via the `massive` package)

Used only when `MASSIVE_API_KEY` is set. Polls the snapshot endpoint for the full watched-ticker set in one REST call rather than one call per ticker, to stay within free-tier rate limits (5 req/min → poll every 15s by default; raise to 2–5s on paid tiers by passing a smaller `poll_interval`).

```python
class MassiveDataSource(MarketDataSource):
    def __init__(self, api_key: str, price_cache: PriceCache, poll_interval: float = 15.0) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)
        await self._poll_once()   # immediate first poll, cache isn't empty on startup
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        if not self._tickers or not self._client:
            return
        try:
            # RESTClient is synchronous; offload to a thread so we don't
            # block the event loop for the duration of the HTTP call.
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    timestamp = snap.last_trade.timestamp / 1000.0  # ms → s
                    self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
                except (AttributeError, TypeError) as e:
                    logger.warning("Skipping snapshot for %s: %s", getattr(snap, "ticker", "???"), e)
        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Don't re-raise — common causes (401 bad key, 429 rate limit,
            # transient network errors) should just retry next interval.

    def _fetch_snapshots(self) -> list:
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS, tickers=self._tickers,
        )
```

`add_ticker`/`remove_ticker` just mutate the in-memory `self._tickers` list — the new ticker appears in the cache after the *next* poll cycle (up to `poll_interval` seconds of latency), which is acceptable given the watchlist-edit use case isn't latency-sensitive.

### Interface parity with the simulator

Both sources seed the cache synchronously on `start()` (simulator: computed locally; Massive: one blocking poll before returning) so `GET /api/stream/prices` never has to special-case "data source still warming up."

---

## 7. Source Selection — `factory.py`

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    return SimulatorDataSource(price_cache=price_cache)
```

Returns an *unstarted* source — the caller is responsible for `await source.start(tickers)` during app startup and `await source.stop()` on shutdown. This keeps the factory free of async side effects and easy to unit test (assert on the returned type without spinning up a background task).

---

## 8. SSE Streaming — `create_stream_router`

`GET /api/stream/prices` returns a `StreamingResponse` over `text/event-stream`. The generator polls `PriceCache.version` every 500ms and only emits a payload when the version changed since the last check — this avoids re-serializing/re-sending an unchanged price set to every connected client every tick, and naturally coalesces multiple cache writes that land in the same 500ms window into a single event.

```python
def create_stream_router(price_cache: PriceCache) -> APIRouter:
    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # disable nginx buffering if proxied
            },
        )
    return router

async def _generate_events(price_cache: PriceCache, request: Request,
                            interval: float = 0.5) -> AsyncGenerator[str, None]:
    yield "retry: 1000\n\n"   # browser auto-reconnect delay
    last_version = -1
    try:
        while True:
            if await request.is_disconnected():
                break
            current_version = price_cache.version
            if current_version != last_version:
                last_version = current_version
                prices = price_cache.get_all()
                if prices:
                    payload = json.dumps({t: u.to_dict() for t, u in prices.items()})
                    yield f"data: {payload}\n\n"
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
```

Event payload shape (full snapshot, not a delta — simpler client logic, and the watchlist is small enough that this is cheap):

```json
data: {"AAPL": {"ticker": "AAPL", "price": 190.42, "previous_price": 190.38, "timestamp": 1751212800.5, "change": 0.04, "change_percent": 0.021, "direction": "up"}, "GOOGL": {...}}
```

`request.is_disconnected()` is what lets the generator exit cleanly (and stop polling) the instant a browser tab closes, rather than leaking a coroutine per dead connection. `EventSource`'s built-in retry (driven by the `retry: 1000` directive) handles reconnection on the client without any app code.

Per the project plan, the stream should push the union of the watchlist and any held positions (so the positions table stays live even for tickers a user removed from the watchlist) — this union is computed by the caller when deciding which tickers to `start()`/`add_ticker()` on the data source; the stream layer itself is agnostic and just serializes whatever is in the cache.

---

## 9. Lifecycle Wiring (FastAPI app startup/shutdown)

```python
from app.market import PriceCache, create_market_data_source, create_stream_router

price_cache = PriceCache()
market_source: MarketDataSource | None = None

@app.on_event("startup")
async def startup() -> None:
    global market_source
    market_source = create_market_data_source(price_cache)
    tickers = get_watchlist_tickers()  # from DB, union with held positions
    await market_source.start(tickers)

@app.on_event("shutdown")
async def shutdown() -> None:
    if market_source:
        await market_source.stop()

app.include_router(create_stream_router(price_cache))
```

Watchlist mutations call through to the live source:

```python
@app.post("/api/watchlist")
async def add_to_watchlist(req: AddTickerRequest) -> ...:
    # ... DB insert ...
    await market_source.add_ticker(req.ticker)

@app.delete("/api/watchlist/{ticker}")
async def remove_from_watchlist(ticker: str) -> ...:
    # only remove from the live source if not also held as a position —
    # positions must keep streaming even after being dropped from the watchlist
    if not has_position(ticker):
        await market_source.remove_ticker(ticker)
    # ... DB delete ...
```

---

## 10. Testing Notes

- **Simulator**: deterministic-shape tests (price stays positive, `step()` returns one entry per ticker, correlation matrix is symmetric PSD, Cholesky succeeds) rather than asserting exact values, since the math is stochastic.
- **Cache**: concurrency tests hammering `update()`/`get_all()` from multiple threads to verify the lock prevents torn reads.
- **Massive client**: `RESTClient`/`get_snapshot_all` mocked; tests cover the happy path, a malformed-snapshot skip, and a poll that raises (verifying the loop survives and retries).
- **Factory**: assert `MASSIVE_API_KEY` env var presence/absence selects the right class, without starting either source.

See `backend/tests/market/` for the actual suite (73 tests as of writing — `test_models.py`, `test_cache.py`, `test_simulator.py`, `test_simulator_source.py`, `test_factory.py`, `test_massive.py`).

A Rich-based terminal demo (`backend/market_data_demo.py`) exercises the simulator end-to-end with a live dashboard, sparklines, and an event log — useful for visually sanity-checking GBM parameters and the shock-event rate.
