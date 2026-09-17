import pytest

from mu2edaq_file_reaper.auth import (
    PREFIX_LEN, TokenCache, generate_token, hash_token, normalise_scopes, scope_allows,
    token_prefix, verify_token,
)


def test_token_roundtrip_and_format():
    t = generate_token()
    assert t.startswith("rpr_") and len(t) > 40
    h = hash_token(t)
    algo, iters, salt, digest = h.split("$")
    assert algo == "pbkdf2" and int(iters) > 1000 and len(salt) == 32 and len(digest) == 64
    assert verify_token(t, h)
    assert not verify_token(t + "x", h)
    assert not verify_token(t, "garbage")
    assert not verify_token(t, "md5$1$s$d")
    assert len(token_prefix(t)) == PREFIX_LEN


def test_scopes():
    assert scope_allows(frozenset({"admin"}), "read")
    assert scope_allows(frozenset({"operate"}), "read")
    assert not scope_allows(frozenset({"read"}), "operate")
    assert not scope_allows(frozenset({"operate"}), "admin")
    assert normalise_scopes("read, operate") == {"read", "operate"}
    assert normalise_scopes([]) == {"read"}
    with pytest.raises(ValueError):
        normalise_scopes(["root"])


def test_token_cache_ttl(fake_clock):
    cache = TokenCache(ttl=10, clock=fake_clock)
    cache.put("tok", (1, "cli", frozenset({"read"})))
    assert cache.get("tok") == (1, "cli", frozenset({"read"}))
    fake_clock.advance(11)
    assert cache.get("tok") is None
    cache.put("tok", (1, "cli", frozenset({"read"})))
    cache.invalidate_id(1)
    assert cache.get("tok") is None
