# FinAlly — AI Trading Workstation

An AI-powered trading workstation with live streaming market data, simulated portfolio trading, and an LLM chat assistant that can analyze positions and execute trades via natural language.

Built entirely by coding agents as a capstone project for an agentic AI coding course.

## Features

- Live price streaming via SSE with green/red flash animations and sparkline charts
- $10k virtual cash, market orders, instant fills
- Portfolio heatmap (treemap), P&L chart, and positions table
- AI chat assistant that analyzes holdings and auto-executes trades
- Watchlist management via UI or natural language

## Quick Start

```bash
cp .env.example .env
# Add OPENROUTER_API_KEY to .env

docker build -t finally .
docker run -v finally-data:/app/db -p 8000:8000 --env-file .env finally
# Open http://localhost:8000
```

Or use the helper scripts:

```bash
./scripts/start_mac.sh        # macOS/Linux
./scripts/start_windows.ps1   # Windows
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | For AI chat (via OpenRouter/Cerebras) |
| `MASSIVE_API_KEY` | No | Real market data; omit to use built-in simulator |
| `LLM_MOCK` | No | `true` for deterministic mock responses (testing) |

## Architecture

Single Docker container on port 8000:

- **Frontend**: Next.js static export, TypeScript, Tailwind CSS
- **Backend**: FastAPI (Python/uv), SSE streaming, SQLite
- **AI**: LiteLLM → OpenRouter → Cerebras with structured outputs
- **Market data**: GBM simulator (default) or Massive/Polygon.io API

## License

See [LICENSE](LICENSE).
# finally
