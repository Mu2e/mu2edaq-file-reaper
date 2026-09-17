import pytest

from mu2edaq_file_reaper.domain import TierConfig, UsageSample
from mu2edaq_file_reaper.watermarks import (
    bytes_to_stop, check_tier_config, evaluate_tiers, highest_active, policy_insufficient,
    resolve_marks, should_continue, simulate_after, tier_is_active, transitions, used_percent,
)

EPS = 1e-6


def tier(name="warning", threshold=80.0, high=0.0, low=10.0, policy="LRU-Compress"):
    return TierConfig(name=name, threshold=threshold, policy=policy, high_water=high, low_water=low)


def test_used_percent_df_convention():
    # reserved blocks: total > used + free
    assert used_percent(100, 45, 45) == 50.0
    assert used_percent(100, 90, 10) == 90.0
    assert used_percent(0, 0, 0) is None
    assert used_percent(100, 0, 0) is None
    assert used_percent(100, -1, 50) is None


def test_resolve_marks_defaults_and_clamps():
    assert resolve_marks(tier()) == resolve_marks(tier())  # frozen/deterministic
    m = resolve_marks(tier(threshold=80, high=0, low=10))
    assert (m.trigger, m.stop, m.clamped) == (80.0, 70.0, ())
    m = resolve_marks(tier(threshold=95, high=10, low=10))
    assert m.trigger == 100.0 and "trigger_100" in m.clamped
    m = resolve_marks(tier(threshold=5, low=10))
    assert m.stop == 0.0 and "stop_0" in m.clamped


@pytest.mark.parametrize("used,was_active,expected", [
    (80 - EPS, False, False), (80.0, False, True), (80 + EPS, False, True),
    (70 - EPS, True, False), (70.0, True, False), (70 + EPS, True, True),
    (75.0, False, False), (75.0, True, True),
])
def test_tier_is_active_truth_table(used, was_active, expected):
    assert tier_is_active(used, resolve_marks(tier()), was_active) is expected


def test_evaluate_tiers_all_none_partial():
    tiers = {"warning": tier("warning", 80), "critical": tier("critical", 90), "full": tier("full", 95)}
    assert evaluate_tiers(50, tiers, frozenset()) == frozenset()
    assert evaluate_tiers(92, tiers, frozenset()) == {"warning", "critical"}
    assert evaluate_tiers(99, tiers, frozenset()) == {"warning", "critical", "full"}
    # hysteresis: critical stays active down to its stop (80), warning to 70
    assert evaluate_tiers(85, tiers, frozenset({"warning", "critical"})) == {"warning", "critical"}
    assert evaluate_tiers(79, tiers, frozenset({"warning", "critical"})) == {"warning"}
    assert evaluate_tiers(69, tiers, frozenset({"warning", "critical"})) == frozenset()


def test_highest_active_and_exclude():
    assert highest_active({"warning", "full", "critical"}) == "full"
    assert highest_active({"warning", "critical"}, exclude=frozenset({"critical"})) == "warning"
    assert highest_active(set()) is None
    assert highest_active({"warning"}, exclude=frozenset({"warning"})) is None


def test_transitions_are_ordered_by_severity():
    up, down = transitions(frozenset({"warning"}), frozenset({"critical", "full"}))
    assert up == ("critical", "full") and down == ("warning",)


def test_should_continue_and_insufficient_boundaries():
    m = resolve_marks(tier())
    assert should_continue(70 + EPS, m) and not should_continue(70.0, m)
    t = tier(threshold=80)
    assert policy_insufficient(80 + EPS, t) and not policy_insufficient(80.0, t)


def test_bytes_to_stop_and_simulate_after():
    u = UsageSample(total=1000, used=900, free=100, used_pct=90.0, ts=0)
    m = resolve_marks(tier(threshold=90, low=10))   # stop 80 -> need 100 bytes
    assert bytes_to_stop(u, m) == 100
    after = simulate_after(u, 100)
    assert after.used == 800 and after.free == 200 and abs(after.used_pct - 80.0) < 1e-9
    assert bytes_to_stop(after, m) == 0
    assert simulate_after(u, 10 ** 9).used == 0


def test_check_tier_config_messages():
    tiers = {"warning": tier("warning", 90), "critical": tier("critical", 85), "full": tier("full", 99, high=5, low=100)}
    msgs = check_tier_config(tiers)
    assert any("do not ascend" in m for m in msgs)
    assert any("trigger" in m and "100" in m for m in msgs)
    assert any("stop point clamped" in m for m in msgs)
    assert check_tier_config({"warning": tier()}) == []
