"""Tests for common/retry.py and common/journal.py."""

import pytest

from common.journal import DuplicateCorrelationId, Journal
from common.retry import CircuitBreaker, CircuitOpenError, with_retry


class TestRetry:
    def test_succeeds_after_transient_failures(self):
        calls = {"n": 0}

        @with_retry(attempts=3, backoff=(0, 0, 0), jitter=False, sleep=lambda _: None)
        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ValueError("transient")
            return "ok"

        assert flaky() == "ok"
        assert calls["n"] == 3

    def test_raises_after_exhausting_attempts(self):
        @with_retry(attempts=2, backoff=(0,), jitter=False, sleep=lambda _: None)
        def always_fails():
            raise ValueError("permanent")

        with pytest.raises(ValueError):
            always_fails()

    def test_circuit_opens_after_threshold_and_recovers(self):
        breaker = CircuitBreaker("test", failure_threshold=2, cooldown_s=1000)

        @with_retry(attempts=1, breaker=breaker, sleep=lambda _: None)
        def fails():
            raise ValueError("boom")

        for _ in range(2):
            with pytest.raises(ValueError):
                fails()
        # threshold reached → circuit open → CircuitOpenError, not ValueError
        with pytest.raises(CircuitOpenError):
            fails()
        breaker.record_success()
        assert not breaker.is_open


class TestJournal:
    def test_duplicate_correlation_id_refused(self, tmp_path):
        j = Journal(tmp_path / "j.db")
        j.record_order("fxc-1", module="fxc", symbol_key="EURUSD",
                       broker_symbol="EURUSD", side="BUY", lots=0.01,
                       order_type="MARKET")
        with pytest.raises(DuplicateCorrelationId):
            j.record_order("fxc-1", module="fxc", symbol_key="EURUSD",
                           broker_symbol="EURUSD", side="BUY", lots=0.01,
                           order_type="MARKET")

    def test_order_lifecycle(self, tmp_path):
        j = Journal(tmp_path / "j.db")
        j.record_order("fxc-2", module="fxc", symbol_key="XAUUSD",
                       broker_symbol="GOLD", side="SELL", lots=0.02,
                       order_type="MARKET", sl=2400.0, tp=2380.0)
        j.update_order("fxc-2", status="TRADED", ticket=111, deal_id=222,
                       fill_price=2392.5, commission=-0.07, terminal=True)
        row = j.get_order("fxc-2")
        assert row["status"] == "TRADED"
        assert row["fill_price"] == 2392.5
        assert row["ts_terminal"] is not None

    def test_halt_persists_across_restart(self, tmp_path):
        """The daily-loss halt must survive a process restart (§9.3)."""
        db = tmp_path / "j.db"
        j1 = Journal(db)
        j1.set_halt("2026-07-08", "daily loss limit")
        j1.close()

        j2 = Journal(db)   # simulated restart
        assert j2.is_halted("2026-07-08")
        assert not j2.is_halted("2026-07-09")

    def test_daily_pnl_accumulates(self, tmp_path):
        j = Journal(tmp_path / "j.db")
        assert j.add_daily_pnl("2026-07-08", -50.0) == -50.0
        assert j.add_daily_pnl("2026-07-08", 120.0) == 70.0
        state = j.get_risk_state("2026-07-08")
        assert state["trade_count"] == 2

    def test_position_roundtrip(self, tmp_path):
        j = Journal(tmp_path / "j.db")
        pid = j.open_position(ticket=999, magic=520025, symbol_key="EURUSD",
                              broker_symbol="EURUSD", direction="LONG",
                              lots=0.01, entry_price=1.0850, stop_loss=1.0820,
                              target=1.0865, signal={"p_hat": 0.86})
        assert len(j.get_open_positions()) == 1
        j.close_position(pid, exit_price=1.0865, realized_pnl=1.5,
                         exit_reason="TP")
        assert j.get_open_positions() == []
