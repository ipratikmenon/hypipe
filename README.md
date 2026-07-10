# hypipe

Algorithmic trading systems: Indian index options via [Dhan](https://dhan.co),
global FX/bullion/commodities via MetaTrader 5.

## Modules

| Module | Status | Broker | Description |
|--------|--------|--------|-------------|
| [`indices/`](indices/) | Built | Dhan | DEMA 10/20/95 crossover options trader for NIFTY, BANKNIFTY, MIDCPNIFTY, FINNIFTY weekly options — standalone polling bot + TradingView webhook automation |
| [`fx_commodities/`](fx_commodities/REQUIREMENTS.md) | Spec complete (v2.0) | MT5 (broker-agnostic adapter) | Orderflow + quote-dynamics prediction engine for FX spot (EURUSD/GBPUSD/USDJPY), bullion (XAUUSD/XAGUSD), and commodity CFDs (WTI/NATGAS/COPPER) — full build spec in REQUIREMENTS.md |
| [`overnight/`](overnight/REQUIREMENTS.md) | Spec complete | Dhan | Overnight-gap engine for NIFTY/BANKNIFTY/MIDCPNIFTY/FINNIFTY — 15:15 IST afternoon-session features → buy option at 15:20, sell at next open, gated by the theta-carry breakeven p* = ½(1+(Θ+c)/(Δ·S·g)); includes news/calendar tracking and the twice-daily signal report |
| `common/` | Built | — | Shared plumbing: retry & circuit breaker, trade journal, alerting |

See [EVOLUTION.md](EVOLUTION.md) for the self-evolving research-loop design, and [PLAN.md](PLAN.md) for the full system audit, hardening roadmap, and the
`fx_commodities/` design.

## Disclaimer

This software is for educational purposes. Algorithmic trading involves
significant financial risk. Always validate in backtests and paper trading
before going live.
