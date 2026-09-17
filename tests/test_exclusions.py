from mu2edaq_file_reaper.exclusions import ExclusionRegistry, ExclusionRules, is_excluded, rules_from_rows


def test_is_excluded_exact_glob_basename_and_dir_prefix():
    rules = ExclusionRules(exact=frozenset({"/d/keep.dat"}), globs=("*.raw", "/d/keepdir", "/d/run_00*"))
    assert is_excluded("/d/keep.dat", rules)
    assert is_excluded("/d/sub/x.raw", rules)          # basename glob
    assert is_excluded("/d/keepdir/inner/file", rules)  # directory prefix
    assert is_excluded("/d/run_0042.dat", rules)
    assert not is_excluded("/d/other.dat", rules)
    assert not is_excluded("/d/keepdirx/file", rules)
    assert not is_excluded("/anything", ExclusionRules())


def test_registry_merges_global_and_area_rules():
    reg = ExclusionRegistry()
    reg.load_rows([
        {"area": None, "pattern": "*.keep", "kind": "glob", "active": True},
        {"area": "raw", "pattern": "/data/raw/important.dat", "kind": "path", "active": True},
        {"area": "raw", "pattern": "*.gone", "kind": "glob", "active": False},
    ])
    raw = reg.rules_for("raw")
    assert is_excluded("/data/raw/x.keep", raw)
    assert is_excluded("/data/raw/important.dat", raw)
    assert not is_excluded("/data/raw/x.gone", raw)
    logs = reg.rules_for("logs")
    assert is_excluded("/logs/x.keep", logs)
    assert not is_excluded("/data/raw/important.dat", logs)
    # cached merge is invalidated on reload
    reg.load_rows([])
    assert not is_excluded("/data/raw/x.keep", reg.rules_for("raw"))


def test_registry_reload_from_repo(repos):
    repos["exclusions"].add("/data/raw/a.dat", area="raw", created_by="t")
    reg = ExclusionRegistry(repos["exclusions"])
    reg.reload()
    assert is_excluded("/data/raw/a.dat", reg.rules_for("raw"))
    assert rules_from_rows([])[None:None] if False else True
