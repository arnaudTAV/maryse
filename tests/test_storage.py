import pytest

from prepress_mcp.storage import StorageError, report_dir, tenant_root


def test_isolation(tmp_path):
    d = report_dir(tmp_path, "acme", "job123")
    assert d.exists()
    assert str(d).startswith(str(tenant_root(tmp_path, "acme")))


@pytest.mark.parametrize("bad", ["../evil", "a/b", "..", "", "with space"])
def test_rejects_traversal(tmp_path, bad):
    with pytest.raises(StorageError):
        report_dir(tmp_path, bad, "job")
    with pytest.raises(StorageError):
        report_dir(tmp_path, "acme", bad)
