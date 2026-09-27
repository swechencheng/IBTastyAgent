# IBTastyAgent

An autonomous AI options-trading agent implementing **tastytrade's premium-selling methodology** executed directly via a local **Interactive Brokers (IBKR) Gateway / TWS** using [`ib_async`](https://github.com/ib-api-reloaded/ib_async). Deterministic tastytrade mechanics act as unyielding guardrails; an OpenRouter LLM (default: `deepseek/deepseek-v4.1-flash`) provides an adaptive selection and sizing layer _within_ those rails.

The agent features native IBKR Market Scanner universe discovery, 1-year historical Implied Volatility (IV) Rank calculation with daily SQLite caching, spread-protective walk-the-book order execution, pre-attached 50% Take-Profit GTC limit orders, strict position isolation (so other portfolio positions are never affected), and a real-time Next.js dashboard.

> ⚠️ **Educational project. Not financial advice.** Options trading involves substantial risk. Test thoroughly in paper trading. Only point it at real capital once you fully understand the mechanics and accept the risks.

---

## How It Works (Detailed Implementation Breakdown)

```
IBKR Market Scanner (OPT_VOLUME_MOST_ACTIVE)
        │
        ▼
1-Year Historical IV Rank & Percentile (OPTION_IMPLIED_VOLATILITY + SQLite cache)
        │
        ▼
Option Chain Resolution & Greeks Streaming (reqSecDefOptParamsAsync + Tick 106)
        │
        ▼
Generate Candidates ─ Strangles · Naked Puts · Put/Call Credit Spreads · Iron Condors
        │
        ▼
HARD GUARDRAILS ── IV Rank (≥30%) · ~45 DTE · ~16Δ Short Legs · Liquidity · Earnings · BP Caps
        │
        ▼
LLM Selects & Sizes (OpenRouter DeepSeek) ── Adaptive layer inside the rails + plain-English rationale
        │
        ▼
POST-LLM RE-VALIDATION ── Guardrails + Sizing + Portfolio Risk rechecked (LLM cannot bypass)
        │
        ▼
IBKR Execution (IBKRPlacer) ── Multi-leg BAG combos · Walk-the-book limit repricing · Pre-attached 50% TP
        │
        ▼
Position Lifecycle & Isolation ── Strict orderRef tracking · 50% TP monitoring · 21 DTE rolls · Untested side rolls
```

### Phase-by-Phase Code Architecture

1. **Universe Discovery via Market Scanner (`backend/tastyagent/ibkr/scanner.py`)**:
   - Executes `reqScannerDataAsync(ScannerSubscription(instrument="STK", locationCode="STK.US.MAJOR", scanCode="OPT_VOLUME_MOST_ACTIVE"))`.
   - Discovers top high-volume, liquid optionable tickers on the market dynamically, replacing static third-party watchlists.
2. **1-Year Historical IV Metrics (`backend/tastyagent/ibkr/metrics.py`)**:
   - Queries IBKR historical market data via `reqHistoricalDataAsync(contract, durationStr="1 Y", barSizeSetting="1 day", whatToShow="OPTION_IMPLIED_VOLATILITY")`.
   - Computes:
     - **IV Rank** = $`\frac{\text{Current IV} - \text{Min IV}_{52w}}{\text{Max IV}_{52w} - \text{Min IV}_{52w}}`$
     - **IV Percentile** = $`\frac{\sum \mathbf{1}(\text{IV}_t < \text{Current IV})}{N}`$
   - Caches calculated metrics into a local SQLite table (`iv_metrics_cache`) with a daily TTL `(symbol, cache_date)` to avoid redundant gateway requests.
3. **Option Chain Resolution & Greeks Streaming (`backend/tastyagent/ibkr/marketdata.py`)**:
   - Fetches underlying spot prices using real-time quotes with automatic fallback to historical daily closes (`reqHistoricalDataAsync`) for non-subscribed exchanges.
   - Resolves active expiration dates and strike grids via `reqSecDefOptParamsAsync`.
   - Discovers option contracts matching the ~45 DTE target and streams Greeks (Delta, Theta, Implied Volatility, Bid, Ask) via generic tick 106 (`reqMktData`).
4. **Candidate Generation & Hard Guardrails (`backend/tastyagent/strategy/candidates.py`, `guardrails.py`)**:
   - Builds candidate structures: Short Strangles, Naked Puts, Put/Call Credit Spreads, and Iron Condors.
   - Enforces deterministic tastytrade rules:
     - Expiration: 30–55 DTE (target 45 DTE).
     - Delta: short legs targeted near 16Δ (max allowable delta cap).
     - Liquidity: bid-ask spread width ratio $\le 10\%$ ($\le 50\%$ in sandbox).
     - Minimum IV Rank: $\ge 30\%$.
     - Earnings Blackout: eliminates underlyings announcing earnings within 7 days.
5. **Adaptive LLM Selection (`backend/tastyagent/decision/llm.py`)**:
   - Formats qualifying candidates and macro regime data into structured JSON.
   - Prompts OpenRouter (default: `deepseek/deepseek-v4.1-flash`) to pick the best risk-adjusted setups and assign capital allocations with clear reasoning.
6. **Post-LLM Re-Validation & Portfolio Sizing (`backend/tastyagent/strategy/sizing.py`, `backend/tastyagent/risk/limits.py`)**:
   - Prevents prompt injection or hallucination: every trade chosen by the LLM is re-verified against guardrails, single-trade buying power caps (max 25%), total portfolio allocation (max 40%), daily loss limits, and consecutive loss halts.
7. **Order Execution & Spread Protection (`backend/tastyagent/ibkr/orders.py`, `backend/tastyagent/ibkr/placer.py`)**:
   - Constructs multi-leg IBKR `BAG` combo contracts (`ComboLeg`).
   - Sells credit spreads/strangles using negative limit prices (`action="BUY"`, `lmtPrice = -credit_per_share`).
   - Employs **walk-the-book** limit repricing starting at favorable mid-price, stepping by 1¢ every N seconds towards natural market price to prevent market-maker gouging.
   - Uses **cancel-and-replace** rather than in-place order modification to eliminate IBKR Warning 105 errors.
   - Auto-attaches a 50% Take-Profit GTC limit order (`action="SELL"`, `lmtPrice = -0.50 * credit_per_share`) upon fill.
8. **Position Lifecycle Management & Strict Isolation (`backend/tastyagent/execution/exit_manager.py`, `backend/tastyagent/portfolio/ledger.py`)**:
   - **Isolation**: Tags all orders and positions with unique identifiers: `orderRef="TastyAgent_{trade_id}"`. The agent never touches or interferes with manual positions or trades from other strategies on the account.
   - **Audit**: Continuously audits open IBTastyAgent positions; if any position lacks an active take-profit order, an alert is surfaced immediately.
   - **Defense**: Monitors positions at 21 DTE for standard rolling, or rolls the untested side when a short strike is breached.

---

## Prerequisites

- **Python 3.11+** and **Node.js 20+**
- **Interactive Brokers Gateway or TWS** running locally or on your local network:
  - API enabled in Gateway/TWS settings (_Settings → API → Settings → Enable ActiveX and Socket Clients_).
  - Socket Port configured (default: `4002` for Paper, `4001` for Live).
  - Trusted IP: ensure `127.0.0.1` (or your client host IP) is added to trusted IP addresses.
- **OpenRouter API Key** (for adaptive trade selection; default model: `deepseek/deepseek-v4.1-flash`).

---

## Setup

### 1. Backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate            # On Windows: .venv\Scripts\activate
pip install -e ".[dev]"              # Installs dependencies including ib_async
cp .env.example .env                 # Configure your credentials
```

### 2. Configuration (`backend/.env`)

Edit `backend/.env` (this file is gitignored — never commit real secrets):

| Variable                     | Description                                                 | Default                        |
| ---------------------------- | ----------------------------------------------------------- | ------------------------------ |
| `TASTYAGENT_MODE`            | `sandbox` (paper auto-place), `live_approval`, `live_auto`  | `sandbox`                      |
| `TASTYAGENT_WORKING_CAPITAL` | Simulated capital base to size trades against ($)           | `10000.0`                      |
| `IBKR_HOST`                  | Host IP for trading IBKR Gateway / TWS                      | `127.0.0.1`                    |
| `IBKR_PORT`                  | Socket port for trading gateway (`4002` paper, `4001` live) | `4002`                         |
| `IBKR_CLIENT_ID`             | Unique client ID for trading connection                     | `45`                           |
| `IBKR_ACCOUNT`               | IBKR Real Account ID (e.g. `U1234567`). Only applies to live trading; ignored in `sandbox` | _(optional, auto-detects)_     |
| `IBKR_DATA_HOST`             | Market data gateway host (if using dual gateway)            | `127.0.0.1`                    |
| `IBKR_DATA_PORT`             | Market data gateway port (e.g. `4001` live data)            | `4001`                         |
| `IBKR_DATA_CLIENT_ID`        | Dedicated client ID for market data streaming               | `46`                           |
| `IBKR_SCAN_CODE`             | IBKR Market Scanner code                                    | `OPT_VOLUME_MOST_ACTIVE`       |
| `IBKR_SCAN_ROWS`             | Number of top symbols to fetch from scanner                 | `25`                           |
| `IBKR_WALK_STEP`             | Repricing increment for walk-the-book ($)                   | `0.01`                         |
| `IBKR_WALK_INTERVAL`         | Seconds to wait between walk-the-book price adjustments     | `5`                            |
| `IBKR_ATTACH_TP`             | Whether to automatically submit 50% Take Profit order       | `true`                         |
| `IBKR_TP_PCT`                | Take profit target percentage                               | `0.50`                         |
| `OPENROUTER_API_KEY`         | Your OpenRouter API key                                     | _(required)_                   |
| `OPENROUTER_MODEL`           | LLM model for trade selection                               | `deepseek/deepseek-v4.1-flash` |

### 3. Frontend

```bash
cd frontend
npm install
```

---

## Running the Application

Open two terminal windows:

```bash
# Terminal 1 — FastAPI Backend
cd backend
source .venv/bin/activate
uvicorn tastyagent.api.app:app --port 3060

# Terminal 2 — Next.js Dashboard
cd frontend
npm run dev                          # Open http://localhost:3066
```

### Dashboard Features

- **Run Cycle**: Triggers an on-demand decision cycle.
- **Auto Mode**: Enables market-hours autonomous scheduling.
- **Kill Switch**: Instantly freezes all new order entries.
- **IBKR Scanner Watchlist**: View live high-options-volume symbols discovered directly by IBKR Market Scanner, alongside calculated IV Rank and Percentiles.
- **Active Positions & Queue**: Inspect open combo positions, attached Take-Profit status, Greeks, and pending orders awaiting manual approval (in `live_approval` mode).
- **Equity Curve & Benchmark**: Real-time performance tracking compared against the S&P 500 (SPY).

---

## CLI Diagnostic Tools (`backend/scripts/`)

| Script                | Purpose                                                                                                          |
| --------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `check_ibkr.py`       | Tests connectivity to IBKR trading and data gateways, verifies account balances, positions, and order isolation. |
| `check_scanner.py`    | Runs IBKR Market Scanner (`OPT_VOLUME_MOST_ACTIVE`) and lists active symbols.                                    |
| `check_metrics.py`    | Computes 1-year historical IV Rank & Percentile for test symbols and verifies SQLite cache performance.          |
| `check_candidates.py` | Fetches live IBKR option chains and Greeks, generating candidate spreads/strangles with guardrail diagnostics.   |
| `check_llm.py`        | Verifies OpenRouter connectivity and tests structured JSON selection with DeepSeek.                              |
| `run_cycle.py`        | Executes one full autonomous cycle against IBKR from the command line.                                           |
| `reset.py`            | Safely cancels all IBTastyAgent working orders (without touching external orders) and resets the local DB.       |

---

## Testing

The codebase includes an extensive automated test suite with full mocks for IBKR and LLM interactions:

```bash
cd backend
source .venv/bin/activate
pytest -q
```

All 118 unit tests validate guardrails, sizing, IBKR order generation, combo pricing mechanics, walk-the-book repricing, take-profit attachment, and risk limits.

---

## Project Structure

```
backend/
├── pyproject.toml                         # Package configuration & dependencies (ib_async, etc.)
├── .env.example                           # Template for configuration parameters
├── scripts/                               # Diagnostic and CLI runners (check_ibkr, run_cycle, etc.)
├── tastyagent/
│   ├── api/                               # FastAPI endpoints, WebSocket feeds, and runtime
│   ├── db/                                # SQLite persistence (SQLAlchemy models and session)
│   ├── decision/                          # Market context, OpenRouter LLM client, and orchestrator
│   ├── execution/                         # Order placement, exit manager, and trade tracker
│   ├── ibkr/                              # IBKR client, market data, scanner, metrics, orders, placer
│   ├── portfolio/                         # Ledger, P/L calculation, and S&P 500 benchmark
│   ├── risk/                              # Portfolio safety limits and capital guardrails
│   └── strategy/                          # Delta/DTE guardrails, candidate builders, and sizing
frontend/                                  # Next.js 14 dashboard with TailwindCSS and shadcn/ui
```
