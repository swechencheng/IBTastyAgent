# IBTastyAgent

An autonomous AI options-trading agent implementing **tastytrade's premium-selling methodology** executed directly via local **Interactive Brokers (IBKR) Gateway / TWS** using [`ib_async`](https://github.com/ib-api-reloaded/ib_async). Deterministic tastytrade mechanics act as unyielding guardrails; an OpenRouter LLM (default: `deepseek/deepseek-v4.1-flash`) provides an adaptive selection and sizing layer _within_ those rails.

The agent features native IBKR Market Scanner universe discovery, 1-year historical Implied Volatility (IV) Rank calculation with daily SQLite caching, market-open IV stabilization delay (15-minute buffer), tastytrade 1/3 Strike Width spread credit rules, live IBKR margin cushion guards, multi-currency forex buying power conversion, spread-protective walk-the-book order execution, pre-attached 50% Take-Profit GTC limit orders, strict position isolation (so other portfolio positions are never affected), and a real-time Next.js dashboard.

> ⚠️ **Educational project. Not financial advice.** Options trading involves substantial risk. Test thoroughly in paper trading. Only point it at real capital once you fully understand the mechanics and accept the risks.

---

## How It Works (Architecture & Trading Mechanics)

```
Market Open Timing (09:45 ET ── +15m IV Stabilization Buffer)
        │
        ▼
IBKR Market Scanner (OPT_VOLUME_MOST_ACTIVE)
        │
        ▼
1-Year Historical IV Rank & Percentile (OPTION_IMPLIED_VOLATILITY + SQLite cache)
        │
        ▼
Option Chain Resolution & Greeks Streaming (reqSecDefOptParamsAsync + Tick 106)
        │
        ▼
Generate Candidates ── Put/Call Credit Spreads · Iron Condors · Strangles · Naked Puts
        │
        ▼
HARD GUARDRAILS ── Margin Cushion (≥30% in Live) · 1/3 Width Rule · IVR (≥30%) · ~45 DTE · ~16Δ Short · Liquidity · Earnings · BP Caps
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

### Phase-by-Phase Implementation

1. **Market Open IV Stabilization Delay (`backend/tastyagent/scheduler.py`)**:
   - The first automated decision cycle on any trading day is delayed until at least 15 minutes after US market open (09:45 ET instead of 09:30 ET).
   - Prevents trading during erratic opening auctions, wide bid-ask spreads, and distorted opening IV readings.
   - Configurable via `TASTYAGENT_SCHEDULER_OPEN_DELAY_MINUTES=15` and dashboard settings.

2. **Universe Discovery via Market Scanner (`backend/tastyagent/ibkr/scanner.py`)**:
   - Executes `reqScannerDataAsync(ScannerSubscription(instrument="STK", locationCode="STK.US.MAJOR", scanCode="OPT_VOLUME_MOST_ACTIVE"))`.
   - Dynamically discovers top liquid, high-options-volume equities and ETFs, replacing static third-party watchlists.

3. **1-Year Historical IV Metrics (`backend/tastyagent/ibkr/metrics.py`)**:
   - Queries IBKR historical market data via `reqHistoricalDataAsync(contract, durationStr="1 Y", barSizeSetting="1 day", whatToShow="OPTION_IMPLIED_VOLATILITY")`.
   - Computes:
     - **IV Rank** = $\frac{\text{Current IV} - \text{Min IV (52w)}}{\text{Max IV (52w)} - \text{Min IV (52w)}}$
     - **IV Percentile** = $\frac{\sum \mathbf{1}(\text{IV}_t \lt \text{Current IV})}{N}$
   - Caches calculated metrics into a local SQLite table (`iv_metrics_cache`) with a daily TTL `(symbol, cache_date)` to avoid redundant gateway requests.

4. **Multi-Currency Account & Forex Buying Power (`backend/tastyagent/ibkr/client.py`)**:
   - Native support for IBKR accounts with non-USD base currencies (EUR, GBP, CHF, HKD, etc.).
   - Fetches live forex market data (`EURUSD`, `GBPUSD`, etc.) to convert cash balances, Net Liquidation value, and buying power to exact USD equivalents.
   - Sizing and risk checks evaluate real-time converted USD buying power.

5. **Option Chain Resolution & Greeks Streaming (`backend/tastyagent/ibkr/marketdata.py`)**:
   - Fetches underlying spot prices using real-time quotes with automatic fallback to historical daily closes (`reqHistoricalDataAsync`) for non-subscribed exchanges.
   - Resolves active expiration dates and strike grids via `reqSecDefOptParamsAsync`.
   - Discovers option contracts matching the 30–55 DTE window (target 45 DTE) and streams Greeks (Delta, Theta, Implied Volatility, Bid, Ask) via generic tick 106 (`reqMktData`).

6. **Candidate Generation & Hard Guardrails (`backend/tastyagent/strategy/candidates.py`, `guardrails.py`)**:
   - Builds candidate structures: Put Credit Spreads, Call Credit Spreads, Iron Condors, Short Strangles, and Naked Puts.
   - **Margin Cushion Guard**: In live trading mode (`live_approval`, `live_auto`), account margin cushion must remain $\ge 30$% (`TASTYAGENT_MIN_CUSHION_PCT`). If cushion falls below 30%, all new trade openings are halted immediately.
   - **1/3 Strike Width Rule**: On defined-risk spreads and iron condors, the collected credit must be at least $\frac{1}{3}$ (33.3%) of the spread width (e.g. at least $1.67 credit on a $5 spread).
   - **Delta Targets**: Short legs targeted near 16–24Δ (max allowable delta cap 25Δ); long wings targeted near 5Δ.
   - **Expiration**: 30–55 DTE (target 45 DTE).
   - **Liquidity**: Bid-ask spread width ratio $\le 10$% ($\le 50$% in sandbox).
   - **Minimum IV Rank**: $\ge 30$%.
   - **Earnings Blackout**: Eliminates underlyings announcing earnings within 7 days.

7. **Adaptive LLM Selection (`backend/tastyagent/decision/llm.py`)**:
   - Formats qualifying candidates, portfolio state, and market regime data into structured JSON.
   - Prompts OpenRouter (default: `deepseek/deepseek-v4.1-flash`) to pick the best risk-adjusted setups and assign contract allocations with concise rationale.

8. **Post-LLM Re-Validation & Portfolio Sizing (`backend/tastyagent/strategy/sizing.py`, `backend/tastyagent/risk/limits.py`)**:
   - Prevents hallucinations or prompt injection: every trade picked by the LLM is re-verified against guardrails, single-trade buying power caps (5%), total portfolio allocation (40%), daily loss limits, and consecutive loss halts.

9. **Order Execution & Spread Protection (`backend/tastyagent/ibkr/orders.py`, `backend/tastyagent/ibkr/placer.py`)**:
   - Constructs multi-leg IBKR `BAG` combo contracts (`ComboLeg`).
   - Sells credit spreads/strangles using negative limit prices (`action="BUY"`, `lmtPrice = -credit_per_share`).
   - Employs **walk-the-book** limit repricing starting at favorable mid-price, stepping by 1¢ every N seconds towards natural market price to prevent market-maker gouging.
   - Uses **cancel-and-replace** rather than in-place order modification to eliminate IBKR Warning 105 errors.
   - Pre-attaches a Take-Profit GTC limit order upon fill: profit target is set to 50% of credit collected or 1/3 of spread width, whichever is smaller.

10. **Position Lifecycle Management & Strict Isolation (`backend/tastyagent/execution/exit_manager.py`, `backend/tastyagent/portfolio/ledger.py`)**:
    - **Isolation**: Tags all orders and positions with unique identifiers: `orderRef="TastyAgent_{trade_id}"`. The agent never touches or interferes with manual positions or trades from other strategies on the account.
    - **Audit**: Continuously audits open IBTastyAgent positions; if any position lacks an active take-profit order, an alert is surfaced immediately.
    - **Defense & Exit Execution**: Evaluates open positions every cycle using an unyielding priority hierarchy (Profit Target $\to$ Ahead-of-Pace $\to$ 21 DTE $\to$ Tested Delta $\to$ Hold).

### 📚 Risk Management & Position Lifecycle Documentation
For in-depth mathematical formulations, holding schedules, decision trees, and code-level walkthroughs across all strategy types, see the comprehensive guide:
- [**Risk Management & Position Lifecycle Guide**](docs/risk_management.md) — Unified reference covering undefined-risk strategies (Short Strangles, Naked Puts/Calls, Straddles) and defined-risk strategies (Iron Condors, Vertical Spreads), detailing 50% TP vs $\min(0.50 \times \text{credit},\; \frac{1}{3} \times \text{width})$ dynamic targets, ahead-of-pace milestone holding tables, 21-DTE duration management, untested side defenses, account cushion guardrails, and emergency stops.

---

## Prerequisites

- **Python 3.11+** and **Node.js 20+**
- **Interactive Brokers Gateway or TWS** running locally or on your local network:
  - API enabled in Gateway/TWS settings (_Settings → API → Settings → Enable ActiveX and Socket Clients_).
  - Socket Port configured:
    - **Paper Account (Sandbox)**: Port `4002` (default client ID `55`).
    - **Live Account**: Port `4001` (default client ID `56`).
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

| Variable | Description | Default |
| --- | --- | --- |
| `TASTYAGENT_MODE` | `sandbox` (paper auto-place), `live_approval`, `live_auto` | `sandbox` |
| `TASTYAGENT_USE_CUSTOM_WORKING_CAPITAL` | When `true`, size positions against custom working capital; when `false`, use IBKR Total Cash | `true` |
| `TASTYAGENT_WORKING_CAPITAL` | Custom capital base to size trades against ($) | `10000.0` |
| `IBKR_SANDBOX_HOST` | Host IP for IBKR Sandbox paper trading gateway | `127.0.0.1` |
| `IBKR_SANDBOX_PORT` | Socket port for IBKR Sandbox paper trading gateway | `4002` |
| `IBKR_SANDBOX_CLIENT_ID` | Dedicated client ID for sandbox paper gateway | `55` |
| `IBKR_LIVE_HOST` | Host IP for IBKR Live real account gateway & market data | `127.0.0.1` |
| `IBKR_LIVE_PORT` | Socket port for IBKR Live real account gateway & market data | `4001` |
| `IBKR_LIVE_CLIENT_ID` | Dedicated client ID for live trading gateway | `56` |
| `IBKR_ACCOUNT` | IBKR Real Account ID (e.g. `U1234567`). Only applies to live trading; ignored in `sandbox` | _(optional, auto-detects)_ |
| `TASTYAGENT_SCHEDULER_OPEN_DELAY_MINUTES` | Minutes after market open (9:30 ET) before making first decision of day (IV stabilization) | `15` |
| `TASTYAGENT_SCHEDULER_INTERVAL_SECONDS` | Scheduler cycle interval in seconds (min 30s) | `300.0` |
| `TASTYAGENT_SCHEDULER_MARKET_HOURS_ONLY` | When `true`, scheduler only runs during regular market hours | `true` |
| `TASTYAGENT_AUTO_START_SCHEDULER` | Automatically start scheduler on daemon launch | `false` |
| `TASTYAGENT_MIN_CUSHION_PCT` | Minimum margin cushion required to open trades in live mode | `0.30` |
| `TASTYAGENT_MIN_CREDIT_WIDTH_RATIO` | 1/3 Strike Width rule: minimum net credit / spread width ratio | `0.333` |
| `IBKR_SCAN_CODE` | IBKR Market Scanner code | `OPT_VOLUME_MOST_ACTIVE` |
| `IBKR_SCAN_ROWS` | Number of top symbols to fetch from scanner | `25` |
| `IBKR_WALK_STEP` | Repricing increment for walk-the-book ($) | `0.01` |
| `IBKR_WALK_INTERVAL` | Seconds to wait between walk-the-book price adjustments | `5` |
| `IBKR_ATTACH_TP` | Whether to automatically submit Take Profit order | `true` |
| `IBKR_TP_PCT` | Take profit target percentage of net credit | `0.50` |
| `OPENROUTER_API_KEY` | Your OpenRouter API key | _(required)_ |
| `OPENROUTER_MODEL` | LLM model for trade selection | `deepseek/deepseek-v4.1-flash` |

### 3. Frontend

```bash
cd frontend
npm install
```

---

## Running the Application

### Development Mode

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

### Production Background Daemons (macOS LaunchAgents)

If configured with launchd services:
```bash
# Restart or status check
launchctl kickstart -k gui/$(id -u)/com.chencheng.tastyagent_backend
launchctl kickstart -k gui/$(id -u)/com.chencheng.tastyagent_frontend
```

### Dashboard Features

- **Run Cycle**: Triggers an on-demand decision cycle.
- **Auto Mode**: Enables market-hours autonomous scheduling with 15-minute market-open delay.
- **Kill Switch**: Instantly freezes all new order entries.
- **IBKR Scanner Watchlist**: View live high-options-volume symbols discovered directly by IBKR Market Scanner, alongside calculated IV Rank and Percentiles.
- **Active Positions & Queue**: Inspect open combo positions, attached Take-Profit status, Greeks, and pending orders awaiting manual approval (in `live_approval` mode).
- **Equity Curve & Benchmark**: Real-time performance tracking compared against the S&P 500 (SPY), with automatic curve rebasing when capital is modified.
- **Multi-Currency Cash Summary**: Displays account base currency, Total Cash in base currency, and USD converted equivalent with live exchange rate.
- **Margin Cushion Indicator**: Real-time IBKR account margin cushion badge with safety status.

---

## CLI Diagnostic Tools (`backend/scripts/`)

| Script | Purpose |
| --- | --- |
| `check_ibkr.py` | Tests connectivity to IBKR trading and data gateways, verifies account balances, cushion, positions, and order isolation. |
| `check_scanner.py` | Runs IBKR Market Scanner (`OPT_VOLUME_MOST_ACTIVE`) and lists active symbols. |
| `check_metrics.py` | Computes 1-year historical IV Rank & Percentile for test symbols and verifies SQLite cache performance. |
| `check_candidates.py` | Fetches live IBKR option chains and Greeks, generating candidate spreads/strangles with guardrail diagnostics. |
| `check_llm.py` | Verifies OpenRouter connectivity and tests structured JSON selection with DeepSeek. |
| `run_cycle.py` | Executes one full autonomous cycle against IBKR from the command line. |
| `reset.py` | Safely cancels all IBTastyAgent working orders (without touching external orders) and resets the local DB. |

---

## Testing

The codebase includes an extensive automated test suite with full mocks for IBKR and LLM interactions:

```bash
cd backend
source .venv/bin/activate
pytest -q
```

All **178 unit tests** pass, validating:
- Decision window timing and 15-minute opening buffer.
- Margin cushion guard halting entries in live mode.
- 1/3 strike width credit ratio filter and dynamic TP targets.
- Multi-currency Forex rate fetching and USD buying power conversion.
- S&P 500 benchmark rebasing and equity curve synchronization.
- Guardrails, sizing, IBKR order generation, BAG combo mechanics, walk-the-book repricing, take-profit attachment, and risk limits.

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
│   ├── portfolio/                         # Ledger, P/L calculation, multi-currency cash, and S&P 500 benchmark
│   ├── risk/                              # Portfolio safety limits, cushion guards, and capital guardrails
│   ├── scheduler.py                       # Market hours loop, open delay window gating, and sleep control
│   └── strategy/                          # Delta/DTE guardrails, 1/3 width rule, candidate builders, and sizing
docs/
└── risk_management.md                     # Comprehensive unified risk management, exit hierarchy, and defense guide
frontend/                                  # Next.js dashboard with custom dark theme design system
```
