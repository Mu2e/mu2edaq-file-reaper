import os

from mu2edaq_file_reaper.config import DEFAULT_EXCLUDE, area_name_from_path, area_to_dict, build_areas


def test_defaults_and_per_area_override(tmp_path):
    root = tmp_path / "vol" / "raw"
    root.mkdir(parents=True)
    cfg = {"defaults": {"thresholds": {"warning": 75}, "settle_seconds": "10m",
                        "tiers": {"warning": {"policy": "Age-Compress", "min_age": "2d"}}},
           "areas": [{"path": str(root), "label": "Raw",
                      "tiers": {"critical": {"min_age": "12h", "low_water": 5}, "full": None}}]}
    areas, issues = build_areas(cfg)
    assert not [i for i in issues if "dropped" in i], issues
    a = areas[0]
    assert a.name == area_name_from_path(str(root)) and a.label == "Raw"
    assert a.settle_seconds == 600
    assert a.tiers["warning"].policy == "Age-Compress" and a.tiers["warning"].min_age == 2 * 86400
    assert a.tiers["warning"].threshold == 75.0
    assert a.tiers["critical"].policy == "LRU-Delete" and a.tiers["critical"].min_age == 43200
    assert a.tiers["critical"].low_water == 5.0 and a.tiers["critical"].threshold == 90.0
    assert "full" not in a.tiers                      # `full: null` disables
    assert a.exclude == DEFAULT_EXCLUDE
    d = area_to_dict(a)
    assert d["tiers"]["critical"]["policy"] == "LRU-Delete"


def test_bad_values_never_raise(tmp_path):
    root = tmp_path / "x" / "y"
    root.mkdir(parents=True)
    cfg = {"bogus": 1, "areas": [
        {"path": str(root), "tiers": {"warning": {"policy": "Nuke"}, "critical": {"threshold": "abc"},
                                      "full": {"min_age": "-1d"}, "extra": {}}, "unknown_key": 1},
        {"label": "no path"},
        "not a mapping",
    ]}
    areas, issues = build_areas(cfg)
    assert len(areas) == 1
    assert areas[0].tiers == {}
    joined = "\n".join(issues)
    assert "unknown top-level key 'bogus'" in joined
    assert "unknown policy" in joined and "bad threshold" in joined and "bad min_age" in joined
    assert "unknown tier 'extra'" in joined and "unknown key 'unknown_key'" in joined
    assert "needs a 'path'" in joined
    assert "monitor-only" in joined


def test_system_roots_and_shallow_paths_are_refused(tmp_path):
    areas, issues = build_areas({"areas": [{"path": "/etc"}, {"path": "/"}, {"path": "/usr"}]})
    assert areas == []
    assert all("system root" in i for i in issues)
    areas, issues = build_areas({"areas": [{"path": "/nonexistent-top-level-dir-xyz"}]})
    assert areas == [] and any("top-level" in i for i in issues)
    areas, issues = build_areas({"areas": [{"path": "/nonexistent-top-level-dir-xyz"}]}, allow_shallow_root=True)
    assert len(areas) == 1 and areas[0].config_errors and "does not exist" in areas[0].config_errors[0]


def test_duplicate_names_and_fts_db(tmp_path):
    root = tmp_path / "a" / "b"
    root.mkdir(parents=True)
    cfg = {"areas": [{"path": str(root), "name": "same", "fts_db": str(tmp_path / "missing.db")},
                     {"path": str(root), "name": "same"}]}
    areas, issues = build_areas(cfg)
    assert len(areas) == 1
    assert any("duplicate" in i for i in issues)
    assert areas[0].fts_db == os.path.realpath(str(tmp_path / "missing.db"))
    assert any("fts_db not found" in e for e in areas[0].config_errors)
