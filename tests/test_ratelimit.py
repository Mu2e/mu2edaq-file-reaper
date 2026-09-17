from mu2edaq_file_reaper.notify.ratelimit import RateLimiter


def test_dedup_window_and_escalation():
    rl = RateLimiter(window=600)
    assert rl.allow("k", "warning", 0) == (True, None)
    assert rl.allow("k", "warning", 10) == (False, "suppressed_dup")
    assert rl.allow("k", "error", 20) == (True, None)          # escalation bypasses dedup
    assert rl.allow("k", "warning", 30) == (False, "suppressed_dup")
    assert rl.allow("k", "warning", 700) == (True, None)        # window elapsed
    assert rl.allow("other", "info", 31) == (True, None)


def test_hourly_cap():
    rl = RateLimiter(window=0, max_per_hour=2)
    assert rl.allow("a", "info", 0)[0]
    assert rl.allow("b", "info", 1)[0]
    assert rl.allow("c", "info", 2) == (False, "suppressed_rate")
    assert rl.allow("c", "info", 3601)[0]
