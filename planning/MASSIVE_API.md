# Massive API (formerly Polygon.io)

Massive rebranded from Polygon.io on October 30, 2025. Existing API keys remained compatible.
Python package: `massive` (v2.8.0+). Docs: https://massive.com/docs

---

## Authentication

All requests use an API key passed to the client constructor:

```python
from massive import RESTClient

client = RESTClient(api_key="YOUR_MASSIVE_API_KEY")
```

The key is sent as a query parameter (`apiKey=...`) on every request. There is no OAuth or session concept.

---

## Rate Limits & Data Delay

| Plan | Rate Limit | Data Delay |
|---|---|---|
| Free / Developer | 5 requests/min | 15-minute delay |
| Starter | Higher | 15-minute delay |
| Advanced | Higher | Real-time |
| Business | Highest | Real-time + Fair Market Value |

**Practical implication for this project:** On the free tier, poll no faster than once every 15 seconds to stay within 5 req/min. The `MassiveDataSource` defaults to `poll_interval=15.0`.

---

## Key Endpoints

### 1. Multi-Ticker Snapshot (primary endpoint)

Returns the latest snapshot for multiple tickers in a single call — the most efficient way to get current prices for a watchlist.

```
GET /v2/snapshot/locale/us/markets/stocks/tickers
```

**Query parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `tickers` | string | No | Comma-separated symbols (e.g. `AAPL,MSFT`). Omit for all tickers. |
| `include_otc` | boolean | No | Include OTC securities. Default: `false`. |

**Python:**

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key="YOUR_KEY")

snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "MSFT", "GOOGL"],
)

for snap in snapshots:
    print(snap.ticker, snap.last_trade.price)
```

**Response schema** (one object per ticker in `tickers[]`):

```json
{
  "count": 1,
  "status": "OK",
  "tickers": [
    {
      "ticker": "AAPL",
      "todaysChange": 1.23,
      "todaysChangePerc": 0.65,
      "updated": 1605192894630916600,
      "day": {
        "o": 190.10, "h": 192.50, "l": 189.80, "c": 191.40,
        "v": 52341000, "vw": 191.20
      },
      "prevDay": {
        "o": 188.50, "h": 191.00, "l": 188.20, "c": 190.17,
        "v": 48200000, "vw": 189.85
      },
      "min": {
        "t": 1684428600000,
        "o": 191.30, "h": 191.50, "l": 191.10, "c": 191.40,
        "v": 120000, "vw": 191.32, "n": 42
      },
      "lastTrade": {
        "p": 191.40,
        "s": 100,
        "t": 1605192894630916600,
        "x": 4,
        "c": [14, 41]
      },
      "lastQuote": {
        "p": 191.38,
        "s": 3,
        "P": 191.42,
        "S": 5,
        "t": 1605192959994246100
      }
    }
  ]
}
```

**Field reference:**

| Field | Description |
|---|---|
| `lastTrade.p` | Last trade price |
| `lastTrade.s` | Last trade size (shares) |
| `lastTrade.t` | Timestamp (Unix **nanoseconds** — divide by 1e9 for seconds) |
| `lastTrade.x` | Exchange ID |
| `lastQuote.p` / `.P` | Bid / Ask price |
| `lastQuote.s` / `.S` | Bid / Ask size |
| `day.o/h/l/c` | Today's open/high/low/close |
| `day.v` | Today's volume |
| `day.vw` | Today's VWAP |
| `prevDay.c` | Previous close (use for daily % change) |
| `min.*` | Most recent minute bar |
| `todaysChangePerc` | % change vs. previous close |

> **Timestamp note:** `lastTrade.t` and `lastQuote.t` are Unix **nanoseconds** in the API JSON, but the Python client exposes them as milliseconds via `snap.last_trade.timestamp`. Divide by 1000 to get Unix seconds for `PriceCache.update()`.

---

### 2. Previous Day Bar (end-of-day)

Returns the prior trading day's OHLCV for a single ticker.

```
GET /v2/aggs/ticker/{stocksTicker}/prev
```

**Parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `stocksTicker` | string | Yes | Ticker symbol (case-sensitive) |
| `adjusted` | boolean | No | Adjust for splits. Default: `true` |

**Python:**

```python
resp = client.get_previous_close(ticker="AAPL")
bar = resp.results[0]
print(bar.close, bar.volume, bar.vwap)
```

**Response:**

```json
{
  "ticker": "AAPL",
  "adjusted": true,
  "resultsCount": 1,
  "status": "OK",
  "results": [{
    "o": 188.50,
    "h": 191.00,
    "l": 188.20,
    "c": 190.17,
    "v": 48200000,
    "vw": 189.85,
    "n": 821432,
    "t": 1605042000000
  }]
}
```

| Field | Description |
|---|---|
| `o` | Open |
| `h` | High |
| `l` | Low |
| `c` | Close |
| `v` | Volume |
| `vw` | Volume-weighted average price |
| `n` | Number of transactions |
| `t` | Start of day timestamp (Unix milliseconds) |

---

### 3. Aggregates / Bars (intraday or historical)

Returns OHLCV bars over a time range at any resolution (minute, hour, day, etc.).

```
GET /v2/aggs/ticker/{stocksTicker}/range/{multiplier}/{timespan}/{from}/{to}
```

**Python:**

```python
bars = []
for bar in client.list_aggs(
    ticker="AAPL",
    multiplier=1,
    timespan="minute",
    from_="2025-06-01",
    to="2025-06-28",
    limit=50000,
):
    bars.append(bar)
```

Pagination is automatic by default.

---

## Error Handling

| HTTP Status | Meaning |
|---|---|
| 200 | OK |
| 401 | Invalid or missing API key |
| 403 | Access denied (endpoint above plan tier) |
| 429 | Rate limit exceeded |
| 4xx | Bad request (invalid ticker, bad params) |

The Python client raises exceptions for non-200 responses. Always wrap polling calls in try/except and retry on the next interval rather than crashing:

```python
try:
    snapshots = client.get_snapshot_all(
        market_type=SnapshotMarketType.STOCKS,
        tickers=tickers,
    )
except Exception as e:
    logger.error("Massive poll failed: %s", e)
    # Will retry on the next interval
```

---

## Running in a Thread

The `RESTClient` is synchronous (blocking HTTP). In an async FastAPI app, run it in a thread to avoid blocking the event loop:

```python
import asyncio

snapshots = await asyncio.to_thread(
    client.get_snapshot_all,
    market_type=SnapshotMarketType.STOCKS,
    tickers=tickers,
)
```
