# Risk Management & Position Lifecycle Guide

This document provides a comprehensive specification of the risk management architecture, position lifecycle controls, profit-taking algorithms, and defensive adjustment mechanics implemented in **IBTastyAgent**.

The agent is designed around **tastytrade's quantitative trading methodology**, prioritizing mechanical discipline, systematic duration management, delta-risk containment, and basis reduction over reactive stop-losses.

---

## 1. Risk Architecture Overview

IBTastyAgent categorizes all options strategies into two fundamental risk paradigms:

| Dimension | Undefined-Risk (Uncapped Risk) | Defined-Risk (Capped Risk) |
| :--- | :--- | :--- |
| **Supported Strategies** | • Short Strangle<br>• Naked Put<br>• Naked Call<br>• Short Straddle | • Put Credit Spread (Bull Put Spread)<br>• Call Credit Spread (Bear Call Spread)<br>• Iron Condor |
| **Max Potential Profit** | Total net credit collected at entry | Total net credit collected at entry |
| **Max Potential Loss** | Unlimited (Call side / Strangle) or Spot to 0 (Put side) | Strictly capped by protective long wing:<br>$\text{Max Loss} = (\text{Strike Width} - \text{Credit}) \times 100 \times \text{Contracts}$ |
| **Margin / Buying Power** | Calculated dynamically by IBKR (~20% notional underlying) | Equal to maximum loss ($\text{Width} - \text{Credit}$) |
| **Probability of Profit (PoP)** | Higher (~68%–84% at 16Δ–24Δ) | Moderate (~65%–75% at 16Δ short / 5Δ long) |
| **Core Management Rule** | **Take profit early at 50%**; roll untested side when tested ($\ge 0.45\Delta$); roll out at 21 DTE for credit; **never roll for a debit**. | **Take profit dynamically** at $\min(50\% \text{ credit},\; \frac{1}{3} \text{ width})$; shift untested wings on Iron Condors; **hold single spreads**; exit cleanly at 21 DTE. |

---

## 2. Global Account Risk Controls & Guardrails

Before evaluating individual position exits, IBTastyAgent enforces system-level safety guardrails ([`backend/tastyagent/risk/`](file:///Users/chencheng.zhang/workspace/trading/TastyAgent/backend/tastyagent/risk/)):

```
Market Session Open
  │
  ├─ 1. Market Open Delay Guard ─────► Wait 15 minutes post-open (09:45 AM ET) for IV stabilization
  │
  ├─ 2. Account Cushion Guard ────────► Check IBKR Cushion >= 30% in Live/Real accounts before opening
  │
  ├─ 3. Account Capital Limits ───────► Enforce Max Portfolio BPR Usage & Capital Allocation
  │
  └─ 4. Symbol Concentration Guard ───► Limit max open trades per underlying asset
```

### 2.1. Live Account Cushion Guard (30% Minimum)
* **Rule**: In live trading mode (`is_paper = False` / `account_type == "real"`), the agent checks IBKR's `cushion` metric. If `cushion < 0.30` (30%), **all new trade openings are blocked immediately**.
* **Rationale**: The IBKR Cushion represents remaining margin headroom relative to net liquidation value ($1 - \text{Margin Usage}$). Requiring at least 30% cushion prevents margin deficiency calls during sudden volatility spikes.
* **Implementation**: Enforced in [`backend/tastyagent/risk/cushion.py`](file:///Users/chencheng.zhang/workspace/trading/TastyAgent/backend/tastyagent/risk/cushion.py) and verified before trade generation in `candidate_selector.py` and `runner.py`.

### 2.2. Market Open 15-Minute Stabilization Window
* **Rule**: During automated scheduling, the agent delays its first new trade decisions until **15 minutes after market open** (09:45 AM ET / 13:45 UTC).
* **Rationale**: The opening 15 minutes exhibit erratic bid-ask spreads, artificial implied volatility distortions, and opening rotation noise. Waiting 15 minutes ensures normalized pricing and stable Greeks before committing capital.
* **Implementation**: Controlled via `open_delay_minutes = 15` in [`backend/tastyagent/scheduler.py`](file:///Users/chencheng.zhang/workspace/trading/TastyAgent/backend/tastyagent/scheduler.py).

### 2.3. Position Isolation & Order Tracking
* **Order Tagging**: Every order is tagged with `orderRef="TastyAgent_{trade_id}"`.
* **Take-Profit Tagging**: Take-profit orders are tagged with `orderRef="TastyAgent_TP_{trade_id}"`.
* **Strict Non-Interference**: The agent only reads and manages positions tagged with its identifier, never interacting with manual trades or external algorithms operating on the same IBKR account.

---

## 3. Position Evaluation Hierarchy (Priority Order)

During each automated execution cycle (`manage_exits` in [`backend/tastyagent/execution/exit_manager.py`](file:///Users/chencheng.zhang/workspace/trading/TastyAgent/backend/tastyagent/execution/exit_manager.py)), open positions are marked to market and evaluated against an **unyielding priority hierarchy** ([`backend/tastyagent/strategy/exits.py`](file:///Users/chencheng.zhang/workspace/trading/TastyAgent/backend/tastyagent/strategy/exits.py)):

```
Position Evaluation (Mark & Greeks Updated)
  │
  ├─ 1. Take-Profit Target (Formula Check) ──────────────────────────► 【CLOSE】 (Take Profit)
  │      ├─ Undefined Risk: 50% of Net Credit
  │      └─ Defined Risk: min(50% Credit, 1/3 Spread Width)
  │
  ├─ 2. Ahead-of-Pace Milestone Schedule (Velocity Check) ──────────► 【CLOSE】 (Early Exit)
  │      └─ Undefined Risk: Days held vs. % profit milestone table
  │
  ├─ 3. Duration Backstop (DTE ≤ 21) ────────────────────────────────► 【ROLL OUT / CLOSE】
  │      ├─ Undefined Risk: Roll to 45 DTE if Net Credit ──────────► 【ROLL OUT】
  │      │                  If no Net Credit available ────────────► 【CLOSE】
  │      └─ Defined Risk:   Vertical spreads cost debits to roll ──► 【CLOSE】 (Clean Exit)
  │
  ├─ 4. Tested-Leg Defense (Delta ≥ 0.45 & DTE > 21) ────────────────► 【DEFENSIVE ACTION】
  │      ├─ Short Strangle: Roll untested short leg toward spot ───► 【ROLL UNTESTED】
  │      │                  (No inversion; if no credit ────────────► 【HOLD】)
  │      ├─ Iron Condor:    Roll untested vertical spread toward spot► 【ROLL UNTESTED】
  │      │                  (Preserve width; no inversion; if no cr ─► 【HOLD】)
  │      ├─ Naked Put / Naked Call: Single leg, no opposite side ──► 【HOLD】
  │      └─ Put / Call Spread: Max loss capped by long wing ───────► 【HOLD】
  │
  ├─ 5. Hard Stop Loss (Optional Catastrophic Stop) ────────────────► 【CLOSE】 (Loss ≥ 2.0x Credit)
  │
  └─ 6. No Trigger Condition Satisfied ──────────────────────────────► 【HOLD】 (Let Theta Decay Work)
```

> **Universal Rule**: **Profit-taking always overrides defense.** If a position reaches 21 DTE or has a tested leg but qualifies for take-profit, the agent **closes the position for a win immediately**, never attempting an unnecessary roll.

---

## 4. Take-Profit Mechanics

### 4.1. Undefined-Risk: 50% Profit Target
For **Short Strangles**, **Naked Puts**, **Naked Calls**, and **Short Straddles**:
* **Target Formula**:
  $$\text{Target Profit Dollars} = 50\% \times \text{Entry Credit}$$
* **Closing Debit**:
  $$\text{TP Limit Price} = \text{Entry Credit} \times (1 - 0.50) = 0.50 \times \text{Entry Credit}$$
* **Rationale**: Research by tastytrade proves that closing undefined-risk trades at 50% max profit significantly elevates overall win rate (~85%+), curtails average days in trade, and eliminates late-cycle tail risk.

### 4.2. Defined-Risk: Dynamic 50% Credit vs. 1/3 Strike Width Rule
For **Put Credit Spreads**, **Call Credit Spreads**, and **Iron Condors**, the agent executes a **dynamic dual-target minimum algorithm** ([`backend/tastyagent/strategy/exits.py`](file:///Users/chencheng.zhang/workspace/trading/TastyAgent/backend/tastyagent/strategy/exits.py#L55-L81)):
$$\text{Target Profit Dollars} = \min\left(50\% \times \text{Entry Credit},\; \frac{1}{3} \times \text{Strike Width} \times 100 \times \text{Contracts}\right)$$

* **Case 1: Standard Spread ($5 width, collected $2.40 credit)**:
  - 50% of Credit = $\$1.20$
  - 1/3 of Spread Width = $\$1.67$
  - **Selected Target** = $\min(\$1.20, \$1.67) = \mathbf{\$1.20}$ (50% credit target).
* **Case 2: Wide Spread / Rich Premium ($5 width, collected $3.60 credit)**:
  - 50% of Credit = $\$1.80$
  - 1/3 of Spread Width = $\$1.67$
  - **Selected Target** = $\min(\$1.80, \$1.67) = \mathbf{\$1.67}$ (1/3 width profit target).
* **Rationale**: Defined-risk spreads with wide strikes or high credits should not remain exposed to late-cycle reversals once they have achieved 1/3 of the spread width.

### 4.3. Pre-Attached GTC Orders & Auto-Healing Audit
* **Instant Submission**: When an opening combo fills, [`IBKRPlacer`](file:///Users/chencheng.zhang/workspace/trading/TastyAgent/backend/tastyagent/ibkr/placer.py) automatically places a Good-'Til-Canceled (GTC) limit order at the computed take-profit price:
  $$\text{Debit Limit} = \text{Fill Credit} - \text{Target Profit}$$
* **Continuous Auto-Healing**: Every decision cycle executes `audit_take_profit_orders` in [`exit_manager.py`](file:///Users/chencheng.zhang/workspace/trading/TastyAgent/backend/tastyagent/execution/exit_manager.py). If an open position lacks a working GTC Take-Profit order on IBKR, the agent automatically generates and submits a replacement GTC order.

---

## 5. Ahead-of-Pace Profit-Taking (Milestone Schedule)

When implied volatility collapses abruptly after entry, waiting for the full target keeps capital tied up needlessly. For undefined-risk trades, the agent implements an **Ahead-of-Pace Milestone Schedule** ([`backend/tastyagent/strategy/profit_schedule.py`](file:///Users/chencheng.zhang/workspace/trading/TastyAgent/backend/tastyagent/strategy/profit_schedule.py)):

| Strategy | Profit Milestone (% Max Profit) | Max Days Held to Qualify | Action |
| :--- | :--- | :--- | :--- |
| **Short Strangle** | **20%**<br>**40%**<br>**50%**<br>**70%**<br>**90%** | $\le$ **6 days**<br>$\le$ **10 days**<br>$\le$ **19 days**<br>$\le$ **28 days**<br>$\le$ **35 days** | Close immediately if profit milestone is achieved at or faster than the target days held. |
| **Naked Put** | **20%**<br>**40%**<br>**50%**<br>**70%**<br>**90%** | $\le$ **2 days**<br>$\le$ **7 days**<br>$\le$ **14 days**<br>$\le$ **26 days**<br>$\le$ **38 days** | Lock in fast directional gains early; eliminate downside assignment risk. |
| **Naked Call** | **20%**<br>**40%**<br>**50%**<br>**70%**<br>**90%** | $\le$ **2 days**<br>$\le$ **7 days**<br>$\le$ **14 days**<br>$\le$ **28 days**<br>$\le$ **43 days** | Short calls are vulnerable to sudden upside momentum; lock in rapid gains quickly. |
| **Short Straddle** | **20%**<br>**40%**<br>**50%**<br>**60%** | $\le$ **16 days**<br>$\le$ **29 days**<br>$\le$ **35 days**<br>$\le$ **42 days** | ATM straddles collect massive credit; managed aggressively against days in trade. |

* **Example**: A Short Strangle opened at 45 DTE gains 25% profit in 4 days. Because 4 days $\le$ 6 days, it hits the 20% milestone and triggers an immediate **CLOSE** to redeploy capital.

---

## 6. 21-DTE Duration Management (The Net Credit Roll Rule)

* **Trigger**: Remaining days to expiration $\le 21$ (`position.dte_remaining <= 21`).
* **Why 21 DTE?**
  1. **Exponential Gamma Escalation**: Inside 21 days, options gamma accelerates rapidly, causing violent P&L swings on small underlying moves.
  2. **Diminishing Theta**: The rate of theta decay slows relative to the sharp increase in delta risk.
  3. **Early Assignment Risk**: ITM or near-the-money short options face heightened early exercise risk.

### 6.1. Undefined-Risk Handling (Roll Out to ~45 DTE)
* The agent searches the option chain for the next standard cycle closest to 45 DTE (range 30–55 DTE).
* Strikes are re-centered to target delta (16Δ–18Δ) based on the current spot price.
* **The Net Credit Mandate**:
  $$\text{New Cycle Credit} \ge \text{Cost to Close Old Cycle}$$
  - **Credit Available**: Close the old trade and simultaneously open the new trade (recorded as a roll event in the ledger).
  - **No Credit Available**: If adverse movement prevents rolling for a net credit, the agent **closes the trade cleanly (`no credit roll; closed`)**, refusing to pay a debit.

### 6.2. Defined-Risk Handling (The Clean Exit Dilemma)
* For **vertical spreads**, rolling out to the next month almost always requires paying a **net debit** because buying the new protective long wing costs more than the decay of the existing short wing.
* **The Rule**: If a net credit roll cannot be established:
  $$\mathbf{Action} = \mathbf{CLOSE} \quad (\text{“21 DTE: no credit roll; closed”})$$
* Defined-risk positions are **cleanly closed out at 21 DTE** to eliminate expiration-week gamma risk, never paying debits to prolong losing spreads.

---

## 7. Tested-Leg Defense Mechanics ($\Delta \ge 0.45$ & DTE > 21)

When a short option leg's absolute delta reaches or exceeds **0.45** (`tested_delta_threshold`) prior to 21 DTE, defensive adjustments are triggered:

### 7.1. Short Strangle Defense: Roll Untested Side Inward
* **Concept**: The tested side has increased in delta, while the untested opposite side has decayed. We harvest the decayed leg and roll it closer to spot to collect more credit and widen overall breakevens.
* **Execution**:
  - **Underlying Rallies (Call Tested, $\Delta_{\text{call}} \ge 0.45$)**: Keep short call intact. Buy back short put and roll the Short Put **UP** towards spot (new delta ~18Δ).
  - **Underlying Drops (Put Tested, $\Delta_{\text{put}} \ge 0.45$)**: Keep short put intact. Buy back short call and roll the Short Call **DOWN** towards spot (new delta ~18Δ).
* **The Strict No-Inversion Rule**:
  $$\text{New Put Strike} < \text{Short Call Strike} \quad \text{and} \quad \text{New Call Strike} > \text{Short Put Strike}$$
  The agent **strictly forbids inverted strangles** (where put strike exceeds call strike), ensuring positive theta and avoiding double-sided intrinsic exposure.
* **Hold Fallback**: If rolling the untested leg does not generate a net credit, the agent does **NOT** force a close. It holds the trade (`ExitOutcome: hold`), waiting for 21 DTE.

### 7.2. Iron Condor Defense: Roll Untested Vertical Spread Inward
* **Concept**: An Iron Condor consists of two vertical credit spreads. The safe, decayed spread is rolled inward to collect additional credit.
* **Execution**:
  - Move the untested short strike closer to spot (~18Δ).
  - **Width Preservation**: Move the long protective wing in exact tandem to preserve the original spread width:
    $$\text{New Long Strike} = \text{New Short Strike} \pm \text{Original Width}$$
  - **No-Inversion Rule**: Short put strike must remain below short call strike.
* **Credit Requirement & Hold Fallback**: Must collect a net credit. If wide bid-ask spreads prevent a credit roll, emit **HOLD** (`no credit roll; holding`).

### 7.3. Single Vertical Spreads & Naked Options: Firm Hold Policy
* **Single Vertical Spreads (Put/Call Credit Spread)**:
  - **No Opposite Wing**: There is no decayed wing to monetize.
  - **No Debit Rolls**: Rolling a single spread out or in strikes almost always requires paying a debit or widening risk.
  - **Max Loss Already Capped**: The long wing contractually limits total loss.
  - **Action**: **HOLD** (`no credit roll; holding`) to allow mean reversion before 21 DTE.
* **Single Naked Options (Naked Put / Naked Call)**:
  - No opposite wing exists to shift.
  - **Action**: **HOLD** until mean reversion occurs or 21 DTE duration management triggers.

---

## 8. Emergency Hard Stop (Optional Safety Valve)

* **Default State**: `params.use_hard_stop = False` (Disabled).
* **tastytrade Rationale**: Mechanical stop-losses in premium selling trigger during peak implied volatility, locking in losses at the exact bottom/top before mean-reversion. Long-term studies show mechanical stops reduce total strategy expectancy.
* **When Configured (`use_hard_stop = True`)**:
  - Stop Threshold:
    $$\text{Unrealized Loss} \le - \text{stop\_loss\_multiple} \times \text{Entry Credit} \quad (\text{Default: } -2.0\times)$$
  - Triggers an immediate `ExitAction.CLOSE` to cap tail risk during historic market shocks.

---

## 9. Master Strategy Risk & Exit Reference Matrix

| Strategy | Profit Target Rule | Ahead-of-Pace? | Tested Defense ($\Delta \ge 0.45$) | 21-DTE Management | Hard Stop (Optional) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Short Strangle** | 50% Net Credit | Yes (Table Schedule) | Roll Untested Short Leg inward (No Inversion; Credit Only; else Hold) | Roll out to 45 DTE for net credit; else Close | $2.0\times$ credit |
| **Naked Put** | 50% Net Credit | Yes (Table Schedule) | **Hold** (No opposite wing; wait for mean-reversion) | Roll out to 45 DTE for net credit; else Close | $2.0\times$ credit |
| **Naked Call** | 50% Net Credit | Yes (Table Schedule) | **Hold** (No opposite wing; wait for mean-reversion) | Roll out to 45 DTE for net credit; else Close | $2.0\times$ credit |
| **Short Straddle** | 50% Net Credit | Yes (Table Schedule) | Evaluated on delta breach; roll to 45 DTE | Roll out to 45 DTE for net credit; else Close | $2.0\times$ credit |
| **Iron Condor** | $\min(50\% \text{ credit},\; \frac{1}{3} \text{ width})$ | No (Uses 1/3 cap) | Roll Untested Vertical Spread inward (Preserve width; No Inversion; Credit only; else Hold) | Attempt net credit roll; else Close cleanly | Max loss capped by strike width; $2.0\times$ opt. |
| **Put Credit Spread** | $\min(50\% \text{ credit},\; \frac{1}{3} \text{ width})$ | No (Uses 1/3 cap) | **Hold** (Max loss capped; no debit rolls) | Close cleanly (debit roll avoidance) | Max loss capped by strike width; $2.0\times$ opt. |
| **Call Credit Spread** | $\min(50\% \text{ credit},\; \frac{1}{3} \text{ width})$ | No (Uses 1/3 cap) | **Hold** (Max loss capped; no debit rolls) | Close cleanly (debit roll avoidance) | Max loss capped by strike width; $2.0\times$ opt. |

---

## 10. Implementation File Map

- **Account Cushion Guard**: [`backend/tastyagent/risk/cushion.py`](file:///Users/chencheng.zhang/workspace/trading/TastyAgent/backend/tastyagent/risk/cushion.py)
- **Market Open Delay**: [`backend/tastyagent/scheduler.py`](file:///Users/chencheng.zhang/workspace/trading/TastyAgent/backend/tastyagent/scheduler.py)
- **Exit Action Evaluation**: [`backend/tastyagent/strategy/exits.py`](file:///Users/chencheng.zhang/workspace/trading/TastyAgent/backend/tastyagent/strategy/exits.py)
- **Ahead-of-Pace Schedule**: [`backend/tastyagent/strategy/profit_schedule.py`](file:///Users/chencheng.zhang/workspace/trading/TastyAgent/backend/tastyagent/strategy/profit_schedule.py)
- **Exit Lifecycle Orchestration & Auto-Audit**: [`backend/tastyagent/execution/exit_manager.py`](file:///Users/chencheng.zhang/workspace/trading/TastyAgent/backend/tastyagent/execution/exit_manager.py)
- **Rolling Execution & Strike Selection**: [`backend/tastyagent/runner.py`](file:///Users/chencheng.zhang/workspace/trading/TastyAgent/backend/tastyagent/runner.py)
- **GTC Take-Profit Order Attachment**: [`backend/tastyagent/ibkr/placer.py`](file:///Users/chencheng.zhang/workspace/trading/TastyAgent/backend/tastyagent/ibkr/placer.py)
