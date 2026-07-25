from prepress_mcp.config import TenantRegistry, Tenant


def _reg():
    return TenantRegistry(
        _by_token={"tok-a": Tenant("acme", "ACME", "tok-a"),
                   "tok-b": Tenant("globex", "Globex", "tok-b")},
        _by_id={"acme": Tenant("acme", "ACME", "tok-a"),
                "globex": Tenant("globex", "Globex", "tok-b")},
    )


def test_resolves_valid_token():
    assert _reg().resolve("tok-a").tenant_id == "acme"


def test_rejects_unknown_token():
    assert _reg().resolve("nope") is None
