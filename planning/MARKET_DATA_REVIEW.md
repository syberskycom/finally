# Market Data Backend — Code Review

**Date:** 2026-06-30
**Scope:** `backend/app/market/` (8 source files, ~500 lines) and `backend/tests/market/` (6 test files, 73 tests)
**Reviewer:** Claude (automated review via GitHub issue #5)

---

## 0. How This Review Was Conducted

This is a **static/manual review** — the test suite was **not executed**. The sandboxed Bash tool available to this automated run requires explicit approval for any `uv`/`python3`/`pytest` invocation (even `uv --version`), and no interactive approval channel exists in this headless GitHub Actions context. Every attempt to run `uv sync`, `uv run pytest`, or even plain `python3 -c "..."` returned `This command requires approval` with no prompt surfaced.

**To actually execute the test suite, add something like `Bash(uv:*)` to `--allowedTools` in `.github/workflows/claude.yml`** and re-trigger this task. Until then, the conclusions below are based on careful reading of every source and test file, cross-checked against the design docs (`MARKET_DATA_DESIGN.md`, `MARKET_INTERFACE.md`, `MARKET_SIMULATOR.md`, `MASSIVE_API.md`) and the prior archived review (`planning/archive/MARKET_DATA_REVIEW.md`), not by running the suite.

Given that constraint, this review focuses on: (1) verifying the 7 issues the prior review flagged were actually fixed in the code as the summary claims, and (2) looking for new issues.

---

## 1. Verification of Previously-Reported Fixes

`planning/MARKET_DATA_SUMMARY.md` claims all 7 issues from the prior review (`planning/archive/MARKET_DATA_REVIEW.md`) were resolved. I checked each against the current code:

| # | Issue | Claimed fix | Verified in code? |
|---|---|---|---|
| 1 | `pyproject.toml` missing wheel packages config (blocks `uv sync`) | Add `[tool.hatch.build.targets.wheel] packages = ["app"]` | ✅ Present at `backend/pyproject.toml:23-24` |
| 2 | `massive` lazy-imported inside methods, breaking `patch()` targets | Move imports to module level | ✅ `massive_client.py:8-9` imports `RESTClient`/`SnapshotMarketType` at module level |
| 3 | `_generate_events` annotated `-> None` despite being a generator | Annotate `-> AsyncGenerator[str, None]` | ✅ `stream.py:55` |
| 4 | `SimulatorDataSource.get_tickers` reached into `GBMSimulator._tickers` (private) | Add public `GBMSimulator.get_tickers()` | ✅ `simulator.py:140-142`, used at `simulator.py:258` |
| 5 | Unused `DEFAULT_CORR` constant, confusing vs. `CROSS_GROUP_CORR` | Remove `DEFAULT_CORR`, consolidate | ✅ `seed_prices.py` only defines `INTRA_TECH_CORR`/`INTRA_FINANCE_CORR`/`CROSS_GROUP_CORR`/`TSLA_CORR` — no `DEFAULT_CORR` |
| 6 | Unused imports (`pytest`, `math`, `asyncio`) in 4 test files | Remove them | ✅ `test_cache.py`, `test_factory.py`, `test_simulator.py` import only what they use; `test_massive.py` no longer imports `asyncio` |
| 7 | Massive test mocks fragile (`source._client` unset, `patch()` target didn't exist) | Set `source._client` explicitly; patch now targets a real module-level name | ✅ All `test_massive.py` tests set `source._client = MagicMock()` before calling `_poll_once()`, and `patch("app.market.massive_client.RESTClient")` now targets a name that genuinely exists at module level (fix #2 makes this patch target real) |

**All 7 previously-reported issues are confirmed fixed in the current code.** Given fix #2 and #7 together, the 5 previously-failing `test_massive.py` tests should now pass once the `massive` package is actually installed (it's a core, non-optional dependency — `uv.lock` pins `massive==2.2.0`).

---

## 2. Architecture Assessment

The subsystem is a clean Strategy-pattern implementation and matches its design docs almost exactly — code and documentation are in sync, which made this review straightforward.

```
MarketDataSource (ABC)
├── SimulatorDataSource  →  GBMSimulator (correlated GBM)
└── MassiveDataSource    →  Polygon.io/Massive REST poller
        │
        ▼
   PriceCache (thread-safe, version-counted)
        │
        ├──→ SSE stream endpoint (/api/stream/prices)
        ├──→ Portfolio valuation (not yet built)
        └──→ Trade execution (not yet built)
```

**Strengths:**
- `PriceUpdate` (`frozen=True, slots=True`) is a correct, cheap-to-allocate immutable value type.
- `PriceCache` correctly serializes writes/reads behind a `threading.Lock` — appropriate since `MassiveDataSource` writes from `asyncio.to_thread`.
- The GBM math is textbook-correct: `S(t+dt) = S(t)·exp((μ − ½σ²)dt + σ√dt·Z)`.
- Cholesky-correlated draws are a nice touch for visual realism without being expensive (`O(n²)` build / `O(n³)` factor, negligible at `n < 50`).
- Both data sources are defensive: `_run_loop`/`_poll_loop` wrap each tick in `try/except Exception` so one bad tick logs and continues rather than killing the only producer.
- `factory.py` is a pure function (no async side effects), easy to unit test — and is tested well.
- The codebase has no `main.py`/app wiring yet, which is expected — per `PLAN.md`, only the market data component is complete; the rest of the platform (DB, portfolio, chat, frontend) is still to be built.

---

## 3. Issues Found

### 3.1 `PriceCache.update()` falsy-timestamp bug (Severity: Low)

`cache.py:30`:
```python
ts = timestamp or time.time()
```
If a caller explicitly passes `timestamp=0.0` (Unix epoch), `0.0 or time.time()` evaluates the right side because `0.0` is falsy — silently discarding the caller's `0.0` and substituting "now". In practice no real market data ever has a `1970-01-01T00:00:00Z` timestamp, so this is very unlikely to bite, but it's a textbook "falsy vs. None" footgun. Prefer `ts = timestamp if timestamp is not None else time.time()`.

### 3.2 Module-level `router` in `stream.py` is a shared mutable singleton (Severity: Low, unresolved from prior review)

`stream.py:17`:
```python
router = APIRouter(prefix="/api/stream", tags=["streaming"])

def create_stream_router(price_cache: PriceCache) -> APIRouter:
    @router.get("/prices")
    async def stream_prices(...): ...
    return router
```
`create_stream_router()` registers a new route on the **same module-level `router` object** every time it's called, rather than constructing a fresh `APIRouter()` per call. If it's ever called more than once in a process — e.g. a future test fixture that builds a fresh FastAPI `app` per test by calling `create_stream_router(cache)` for each test's own `PriceCache` — `/prices` accumulates duplicate route registrations, with later closures (bound to whichever `price_cache` was passed at that call) shadowing or coexisting unpredictably. This was flagged in the prior review and is still present. Low risk today since the function is only ever called once at real app startup, but it's a latent footgun for the test suite that doesn't exist yet for `stream.py`. Fix: build `APIRouter()` inside the factory function.

### 3.3 `_rebuild_cholesky()` has no error handling, and the correlation model isn't proven PSD for arbitrary ticker combinations (Severity: Low)

`simulator.py:154-172` rebuilds and Cholesky-factors the correlation matrix on every `add_ticker`/`remove_ticker` call, with no `try/except`. `np.linalg.cholesky` raises `LinAlgError` if the matrix isn't positive-definite. The current pairwise rule (intra-tech 0.6, intra-finance 0.5, everything else — including all dynamically-added tickers via chat, e.g. a user adding `PYPL` — 0.3) is a reasonable block-equicorrelation structure that's very likely PSD in practice, but nothing in the code or tests proves it stays PSD as more arbitrary tickers are added at runtime (the watchlist is explicitly described in `PLAN.md` as user-editable via chat, so the ticker set isn't bounded to the curated 10). If it ever does throw, the exception is **not** caught — `SimulatorDataSource.add_ticker()` (`simulator.py:242-249`) has no try/except, so it would propagate up to whatever route handler calls `market_source.add_ticker(...)`, unlike `_run_loop`'s `step()` calls which are defensively wrapped. Combined with finding 3.5 below (no test ever exercises more than 2 simulated tickers), this path is essentially unverified for the realistic case of a watchlist that grows past the default 10.

### 3.4 No SSE integration test exists (Severity: Low, unresolved from prior review)

`stream.py` (the actual consumer-facing endpoint, `GET /api/stream/prices`) has no dedicated test file. The prior review flagged this as "nice to have" at 31% coverage; it remains unaddressed — there is still no `test_stream.py`. A basic `httpx.AsyncClient`-driven test (assert the SSE payload shape, the `retry:` directive, and that disconnection via `request.is_disconnected()` stops the generator) would meaningfully de-risk the one piece of this subsystem the frontend will actually talk to.

### 3.5 Missing test: `GBMSimulator` with the full 10-ticker default set (Severity: Low, unresolved from prior review)

`test_simulator.py` only ever constructs `GBMSimulator` with 1–2 tickers. Nothing exercises `GBMSimulator(tickers=list(SEED_PRICES.keys()))` — the actual real-world startup case (`DEFAULT_TICKERS` in `MARKET_INTERFACE.md`) — to confirm the full correlation matrix (mixing the 7-ticker tech group, 2-ticker finance group, and TSLA) is PSD and Cholesky succeeds. Related to 3.3.

### 3.6 No concurrency test for `PriceCache` (Severity: Trivial, unresolved from prior review)

The design doc explicitly justifies using a plain `threading.Lock` because `MassiveDataSource` writes via `asyncio.to_thread` while the simulator writes from the event loop and SSE reads from the event loop — a genuinely concurrent access pattern. No test spins up multiple threads hammering `update()`/`get_all()` to empirically verify the lock prevents torn reads. Low risk (the lock usage is straightforward and clearly correct on inspection), but it's the one piece of this subsystem where "looks correct" and "is correct under load" can diverge.

### 3.7 `massive` package API surface is unverified without network access (Severity: Informational)

`massive_client.py` assumes `snap.last_trade.price`, `snap.last_trade.timestamp` (milliseconds), `client.get_snapshot_all(market_type=..., tickers=...)`, and `SnapshotMarketType.STOCKS` — all documented in `planning/MASSIVE_API.md` and consistent with the code. However, this reviewer could not install the `massive` package (network access blocked in this sandbox, see §0) to confirm the real package's API matches what `MASSIVE_API.md` documents and what the mocks in `test_massive.py` assume. This isn't a code defect, but it's the one area where "tests pass" and "works against the real API" could diverge — worth a one-time manual smoke test against a real (or free-tier) Massive API key before relying on it in production.

---

## 4. Things Done Well (beyond the prior review's list)

- **Design-doc/code parity is excellent.** Every function signature, constant, and behavior described in `MARKET_DATA_DESIGN.md` and `MARKET_INTERFACE.md` matches the actual code exactly — including subtle details like the `mu/sigma` per-ticker overrides and the "seed cache synchronously in `start()`" behavior on both sources. This makes the docs trustworthy as a reference, which is not always true after iteration.
- **Test naming and structure is consistent and readable** across all 6 test files — one assertion-focused concept per test, descriptive docstrings, no over-mocking.
- **`test_massive.py`'s malformed-snapshot test** (`test_malformed_snapshot_skipped`) is a good defensive-programming test: it verifies one bad snapshot in a batch doesn't poison the other tickers' updates.
- **Asymmetric `add_ticker`/`remove_ticker` cache behavior in `MassiveDataSource`** (add doesn't seed the cache immediately, remove evicts immediately) is a deliberate, documented, sensible tradeoff — not a bug, and it's correctly reflected in the tests (`test_add_ticker` doesn't assert a cache value; `test_remove_ticker` does).

---

## 5. Test Suite Assessment (static read, not executed)

73 tests across 6 files, organized one-to-one with source modules:

| Module | Tests | What's covered | What's not |
|---|---|---|---|
| `test_models.py` | 11 | `PriceUpdate` properties, immutability, serialization | — |
| `test_cache.py` | 13 | CRUD, versioning, rounding, dunder methods | Concurrent access (§3.6) |
| `test_simulator.py` | 17 | GBM shape/positivity, ticker add/remove, correlation rules, rounding | Full 10-ticker matrix (§3.5) |
| `test_simulator_source.py` | 10 | Async lifecycle: start/stop/add/remove/idempotency | — |
| `test_factory.py` | 7 | Env-var-driven source selection | — |
| `test_massive.py` | 13 | Poll happy path, malformed snapshot, API errors, ticker normalization, start/stop lifecycle | Real `massive` package wire format (§3.7) |
| `stream.py` (no test file) | 0 | — | SSE endpoint entirely (§3.4) |

Based on the code-level analysis in §1, I expect all 73 tests to pass once dependencies are installed (`uv sync --extra dev`) — the previously-failing 5 `test_massive.py` tests should now pass given the lazy-import fix. **This is an expectation based on static reading, not a verified result** — see §0 for why the suite couldn't be executed here.

---

## 6. Verdict

The market data backend remains solid, well-structured, and well-tested for what it covers. All 7 issues from the prior review were genuinely fixed, not just marked resolved in the summary doc. No new high- or medium-severity issues were found in this pass — everything below is low severity or a coverage gap rather than a functional defect.

**Should fix (low effort, real footguns):**
1. `PriceCache.update()`'s `timestamp or time.time()` falsy-zero bug (§3.1) — one-line fix.
2. `stream.py`'s module-level `router` singleton (§3.2) — move `APIRouter()` construction inside `create_stream_router()`.

**Worth adding before this subsystem sees a real, growing watchlist:**
3. A test constructing `GBMSimulator` with the full default 10-ticker set, to prove the correlation matrix is PSD (§3.5).
4. Error handling (or at least a test) around `_rebuild_cholesky()` for arbitrary/large dynamically-added ticker sets (§3.3).
5. At least one SSE integration test for `stream.py` (§3.4).

**Process note:** this review could not execute `uv sync` / `uv run pytest` because the sandbox denies all `uv`/`python3` invocations without an approval channel that doesn't exist in this headless context. If exact pass/fail counts and coverage percentages are needed (not just the static analysis above), re-run with `Bash(uv:*)` added to `--allowedTools`.
