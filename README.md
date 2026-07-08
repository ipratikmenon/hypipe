# hypipe

Algorithmic trading systems built on the [Dhan](https://dhan.co) broker API.

## Modules

| Module | Status | Description |
|--------|--------|-------------|
| [`indices/`](indices/) | Built | DEMA 10/20/95 crossover options trader for NIFTY, BANKNIFTY, MIDCPNIFTY, FINNIFTY weekly options — standalone polling bot + TradingView webhook automation |
| [`fx_commodities/`](fx_commodities/REQUIREMENTS.md) | Spec complete | Orderflow + liquidity-heatmap prediction engine for currency futures (cross + INR pairs), bullion minis (GOLDM/SILVERM), and commodities (CRUDEOILM/NATURALGAS/COPPER) — full build spec in REQUIREMENTS.md |
| `common/` | Planned | Shared plumbing: Dhan auth/token renewal, retry & circuit breaker, trade journal |

See [PLAN.md](PLAN.md) for the full system audit, hardening roadmap, and the
`fx_commodities/` design.

## Disclaimer

This software is for educational purposes. Algorithmic trading involves
significant financial risk. Always validate in backtests and paper trading
before going live.
