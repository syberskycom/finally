# Market Simulator

The simulator generates realistic-looking stock prices without any external API. It is the default data source when `MASSIVE_API_KEY` is not set.

Implementation: `backend/app/market/simulator.py` and `backend/app/market/seed_prices.py`.

---

## Design Goals

- Prices should look plausible: continuous, no sudden jumps except occasional "events"
- Stocks in the same sector should move somewhat together (tech stocks correlate)
- Each ticker should have its own volatility character (TSLA moves more than JPM)
- Updates every 500ms for a fluid, live-feeling UI

---

## Price Generation: Geometric Brownian Motion

Each price step follows the GBM formula:

```
S(t + dt) = S(t) * exp((mu - 0.5 * sigma²) * dt + sigma * sqrt(dt) * Z)
```

| Symbol | Meaning |
|---|---|
| `S(t)` | Current price |
| `mu` | Annualized drift (expected return, e.g. 0.05 = 5%/yr) |
| `sigma` | Annualized volatility (e.g. 0.25 = 25%/yr) |
| `dt` | Time step as a fraction of a trading year |
| `Z` | Standard normal random variable (correlated across tickers) |

**Why GBM?** It's the standard model for equity prices: prices are always positive (log-normal distribution), changes compound multiplicatively, and the math is simple and fast.

### Time step

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # = 5,896,800
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ≈ 8.48e-8
```

500ms expressed as a fraction of a trading year. This tiny `dt` produces sub-cent moves per tick that accumulate naturally over time, matching how real intraday prices behave.

---

## Correlated Moves (Cholesky Decomposition)

Stocks in the same sector tend to move together. To model this, correlated random draws are generated via Cholesky decomposition of a correlation matrix.

### Correlation structure

```python
INTRA_TECH_CORR    = 0.6   # AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX
INTRA_FINANCE_CORR = 0.5   # JPM, V
CROSS_GROUP_CORR   = 0.3   # between sectors, or unknown tickers
TSLA_CORR          = 0.3   # TSLA is in tech but does its own thing
```

### How it works

1. Build an `n×n` correlation matrix from pairwise correlations
2. Compute its Cholesky factor `L` (lower triangular, so `L @ L.T = corr`)
3. Each tick: draw `n` independent standard normals `z`, then compute `L @ z` to get correlated draws
4. Each ticker uses its own correlated draw as the `Z` in the GBM formula

```python
z_independent = np.random.standard_normal(n)
z_correlated  = self._cholesky @ z_independent  # now correlated
```

The Cholesky matrix is rebuilt whenever tickers are added or removed (`O(n²)`, negligible for `n < 50`).

---

## Random Shock Events

Every tick, each ticker has a small independent chance of a sudden large move:

```python
EVENT_PROBABILITY = 0.001   # 0.1% per tick per ticker
SHOCK_RANGE       = (0.02, 0.05)  # 2–5% move, random direction
```

With 10 tickers at 2 ticks/second, expect roughly one shock event every ~50 seconds — enough to keep the UI interesting without overwhelming it.

---

## Seed Prices and Per-Ticker Parameters

Initial prices and GBM parameters (`seed_prices.py`):

```python
SEED_PRICES = {
    "AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00,
    "AMZN": 185.00, "TSLA": 250.00, "NVDA": 800.00,
    "META": 500.00, "JPM":  195.00, "V":    280.00,
    "NFLX": 600.00,
}

TICKER_PARAMS = {
    "AAPL": {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT": {"sigma": 0.20, "mu": 0.05},
    "AMZN": {"sigma": 0.28, "mu": 0.05},
    "TSLA": {"sigma": 0.50, "mu": 0.03},  # high volatility
    "NVDA": {"sigma": 0.40, "mu": 0.08},  # high volatility, strong drift
    "META": {"sigma": 0.30, "mu": 0.05},
    "JPM":  {"sigma": 0.18, "mu": 0.04},  # low volatility (bank)
    "V":    {"sigma": 0.17, "mu": 0.04},  # low volatility (payments)
    "NFLX": {"sigma": 0.35, "mu": 0.05},
}

DEFAULT_PARAMS = {"sigma": 0.25, "mu": 0.05}  # for unknown tickers
```

Unknown tickers added dynamically get `DEFAULT_PARAMS` and a random seed price between $50–$300.

---

## Code Structure

### `GBMSimulator` (pure math, no I/O)

```python
class GBMSimulator:
    def __init__(self, tickers: list[str], dt: float, event_probability: float)

    def step(self) -> dict[str, float]:
        """Advance all tickers one time step. Returns {ticker: new_price}.
        Hot path — called every 500ms. No I/O, no async."""

    def add_ticker(self, ticker: str) -> None:
        """Add ticker to simulation; rebuilds Cholesky matrix."""

    def remove_ticker(self, ticker: str) -> None:
        """Remove ticker; rebuilds Cholesky matrix."""

    def get_price(self, ticker: str) -> float | None
    def get_tickers(self) -> list[str]
```

`GBMSimulator` is synchronous and has no I/O. It only does math. Easy to unit test.

### `SimulatorDataSource` (async adapter)

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5,
                 event_probability: float = 0.001)

    async def start(self, tickers: list[str]) -> None:
        # Creates GBMSimulator, seeds cache with initial prices,
        # starts asyncio background task

    async def stop(self) -> None:
        # Cancels background task

    async def add_ticker(self, ticker: str) -> None:
        # Delegates to GBMSimulator, seeds cache immediately

    async def remove_ticker(self, ticker: str) -> None:
        # Delegates to GBMSimulator, evicts from cache

    def get_tickers(self) -> list[str]
```

The background loop:

```python
async def _run_loop(self) -> None:
    while True:
        prices = self._sim.step()                      # pure math
        for ticker, price in prices.items():
            self._cache.update(ticker, price)          # write to cache
        await asyncio.sleep(self._interval)            # 500ms
```

---

## Step-by-Step: What Happens Each Tick

1. Draw `n` independent standard normals
2. Multiply by Cholesky factor → correlated normals
3. For each ticker: apply GBM formula → new price
4. For each ticker: 0.1% chance → apply 2–5% random shock (independent of GBM)
5. Round to 2 decimal places
6. Write all new prices to `PriceCache`
7. Sleep 500ms

---

## Testing

```bash
cd backend
uv run --extra dev pytest tests/market/test_simulator.py -v       # 17 unit tests
uv run --extra dev pytest tests/market/test_simulator_source.py   # 10 integration tests
```

Key things tested: GBM produces positive prices, prices change every tick, correlation structure is built correctly, Cholesky rebuilds on add/remove, shock events apply within expected range, `SimulatorDataSource` lifecycle (start/stop/add/remove).

---

## Demo

```bash
cd backend
uv run market_data_demo.py
```

Runs a Rich terminal dashboard with all 10 tickers, live prices, sparklines, direction arrows, and an event log. Runs 60 seconds or until Ctrl+C.
