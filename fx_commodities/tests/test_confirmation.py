"""Zone-confirmation gate tests (§9.1b) — the user's wait-for-close rule."""

import pytest

from fx_commodities.confirmation import Bar, ConfState, ZoneConfirmationGate
from fx_commodities.features.zones import KIND_RESISTANCE, KIND_SUPPORT, Zone


def support_zone(lo=99.0, hi=100.0) -> Zone:
    return Zone(center=(lo + hi) / 2, lo=lo, hi=hi, kind=KIND_SUPPORT,
                source="pivot", created_ts_ms=0)


def bar(ts, o, h, l, c) -> Bar:
    return Bar(ts_close_ms=ts, open=o, high=h, low=l, close=c)


class TestConfirmationStateMachine:
    def test_two_confirming_closes_with_wick_emit_entry(self):
        gate = ZoneConfirmationGate(n_confirm=2, min_wick_ratio=0.5)
        z = support_zone()
        # touch bar: dips into zone, closes back above with a defence wick
        assert gate.on_bar_close([z], bar(1, 100.6, 100.8, 99.4, 100.6)) == []
        # second confirming close above zone → signal on THIS close, not before
        sigs = gate.on_bar_close([z], bar(2, 100.6, 100.9, 100.2, 100.7))
        assert len(sigs) == 1
        sig = sigs[0]
        assert sig.direction == "LONG"
        assert sig.confirm_ts_ms == 2          # emitted only at bar close
        assert sig.touch_price == z.hi
        assert sig.stop_loss < z.lo            # anchored beyond the zone
        assert z.holds == 1

    def test_no_entry_while_price_sits_inside_zone(self):
        """Being at the right price is NOT enough — the user's core rule."""
        gate = ZoneConfirmationGate(n_confirm=2)
        z = support_zone()
        for ts in range(1, 6):   # five bars living inside the zone
            sigs = gate.on_bar_close([z], bar(ts, 99.5, 99.9, 99.2, 99.5))
            assert sigs == []

    def test_violation_close_flips_zone_and_rearms(self):
        gate = ZoneConfirmationGate(n_confirm=2, violate_frac=0.5)
        z = support_zone(99.0, 100.0)           # width 1 → violation at 98.5
        gate.on_bar_close([z], bar(1, 100.2, 100.3, 99.5, 100.2))  # touch
        sigs = gate.on_bar_close([z], bar(2, 99.4, 99.5, 98.0, 98.3))
        assert sigs == []
        assert z.kind == KIND_RESISTANCE        # broken support flips role
        assert z.flips == 1
        assert z.broken is False                # re-armed in the new role

    def test_break_and_retest_produces_continuation_entry(self):
        """The classic pattern: support breaks, price retests the flipped
        zone from below, gets rejected twice → SHORT continuation signal."""
        gate = ZoneConfirmationGate(n_confirm=2, violate_frac=0.5,
                                    min_wick_ratio=0.5)
        z = support_zone(99.0, 100.0)
        # break: close well below violation level (98.5)
        gate.on_bar_close([z], bar(1, 99.6, 99.7, 98.0, 98.2))
        assert z.kind == KIND_RESISTANCE and not z.broken
        # retest from below: wick into the flipped zone, close back under it
        gate.on_bar_close([z], bar(2, 98.4, 99.4, 98.2, 98.5))   # confirm 1
        sigs = gate.on_bar_close([z], bar(3, 98.5, 98.9, 98.1, 98.3))
        assert len(sigs) == 1
        assert sigs[0].direction == "SHORT"
        assert sigs[0].stop_loss > z.hi          # stop beyond the flipped zone

    def test_second_violation_retires_the_zone(self):
        gate = ZoneConfirmationGate(n_confirm=2, violate_frac=0.5)
        z = support_zone(99.0, 100.0)
        gate.on_bar_close([z], bar(1, 99.6, 99.7, 98.0, 98.2))   # flip #1
        assert not z.broken
        # now violate the resistance role: close far above hi + tol (100.5)
        gate.on_bar_close([z], bar(2, 100.2, 101.6, 100.1, 101.5))
        assert z.flips == 2
        assert z.broken is True                  # whipsaw both ways → retired

    def test_intrazone_close_resets_confirm_count(self):
        gate = ZoneConfirmationGate(n_confirm=2, min_wick_ratio=0.0)
        z = support_zone()
        gate.on_bar_close([z], bar(1, 100.4, 100.6, 99.5, 100.4))  # confirm #1
        gate.on_bar_close([z], bar(2, 100.2, 100.3, 99.3, 99.6))   # back inside
        # next confirming close is count 1 again, not 2 → no signal yet
        assert gate.on_bar_close([z], bar(3, 100.3, 100.5, 99.6, 100.4)) == []
        sigs = gate.on_bar_close([z], bar(4, 100.4, 100.6, 100.1, 100.5))
        assert len(sigs) == 1

    def test_wick_requirement_blocks_wickless_confirmation(self):
        gate = ZoneConfirmationGate(n_confirm=2, min_wick_ratio=0.5)
        z = support_zone()
        # closes above zone but always near the low of each bar (no defence)
        gate.on_bar_close([z], bar(1, 100.1, 101.5, 99.9, 100.1))
        sigs = gate.on_bar_close([z], bar(2, 100.1, 101.6, 100.05, 100.2))
        assert sigs == []                       # count met, wick never seen

    def test_entry_window_expires(self):
        gate = ZoneConfirmationGate(n_confirm=1, entry_window_bars=2,
                                    min_wick_ratio=0.0)
        z = support_zone()
        sigs = gate.on_bar_close([z], bar(1, 100.4, 100.6, 99.5, 100.4))
        assert len(sigs) == 1
        assert gate.entry_window_open(z)
        gate.on_bar_close([z], bar(2, 100.5, 100.7, 100.3, 100.6))
        gate.on_bar_close([z], bar(3, 100.6, 100.8, 100.4, 100.7))
        assert not gate.entry_window_open(z)

    def test_resistance_symmetry(self):
        gate = ZoneConfirmationGate(n_confirm=2, min_wick_ratio=0.5)
        z = Zone(center=100.5, lo=100.0, hi=101.0, kind=KIND_RESISTANCE,
                 source="pivot", created_ts_ms=0)
        gate.on_bar_close([z], bar(1, 99.4, 100.6, 99.2, 99.4))    # touch+reject
        sigs = gate.on_bar_close([z], bar(2, 99.4, 99.8, 99.1, 99.3))
        assert len(sigs) == 1
        assert sigs[0].direction == "SHORT"
        assert sigs[0].stop_loss > z.hi


class TestConfirmationEconomics:
    def test_breakeven_uplift_formula(self):
        """Δp must exceed δ/(π+σ) — §9.1b closed form, D5 barriers."""
        pi, sigma, delta = 0.8, 1.6, 0.3
        required_dp = delta / (pi + sigma)
        assert required_dp == pytest.approx(0.125)
        # confirmation value at exactly the threshold is zero
        assert required_dp * (pi + sigma) - delta == pytest.approx(0.0)
