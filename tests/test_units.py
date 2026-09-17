import pytest

from mu2edaq_file_reaper.units import UnitError, parse_duration, parse_percent, parse_size


@pytest.mark.parametrize("text,expected", [
    ("500 GiB", 500 * 1024 ** 3), ("2TB", 2 * 1000 ** 4), ("1.5 gb", 1_500_000_000),
    (1048576, 1048576), ("0", 0), ("10 KiB", 10240), ("3 pb", 3 * 1000 ** 5),
])
def test_parse_size(text, expected):
    assert parse_size(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "-1", "5 parsecs", True, None, "10%"])
def test_parse_size_rejects(bad):
    with pytest.raises(UnitError):
        parse_size(bad)


@pytest.mark.parametrize("text,expected", [(80, 80.0), ("80", 80.0), ("80%", 80.0), ("12.5 %", 12.5), (100, 100.0)])
def test_parse_percent(text, expected):
    assert parse_percent(text) == expected


@pytest.mark.parametrize("bad", [0, "0%", 101, "-5", "abc", True])
def test_parse_percent_rejects(bad):
    with pytest.raises(UnitError):
        parse_percent(bad)


def test_parse_percent_inclusive_zero_for_water_marks():
    assert parse_percent(0, inclusive_lo=True) == 0.0
    with pytest.raises(UnitError):
        parse_percent(-1, inclusive_lo=True)


@pytest.mark.parametrize("text,expected", [
    ("90s", 90), ("10m", 600), ("6h", 21600), ("7d", 604800), ("2w", 1209600), (300, 300), ("1.5h", 5400),
])
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("bad", ["", "-3d", "5 fortnights", True])
def test_parse_duration_rejects(bad):
    with pytest.raises(UnitError):
        parse_duration(bad)
