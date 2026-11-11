"""Lease unit tests: the dynamic spec catalog, with stub colab
callables (no subprocess)."""

import json

from unit_harness import HELP_TEXT, cp, gate_permissive, lease, repo  # noqa: F401


def test_parse_cli_shapes_reads_the_real_help():
    shapes = lease.parse_cli_shapes(HELP_TEXT)
    assert shapes["gpu"] == ["T4", "L4", "G4", "H100", "A100"]
    assert shapes["tpu"] == ["v5e1", "v6e1"]
    assert shapes["no_high_mem"] == ["L4", "v5e1", "v6e1"]


def test_parse_cli_shapes_rejects_unfamiliar_help():
    assert lease.parse_cli_shapes("Usage: colab new [OPTIONS]\nno variants here") is None
    assert lease.parse_cli_shapes("") is None


def _help_stub(help_text=HELP_TEXT):
    def stub(*a, **k):
        if a[:2] == ("new", "--help"):
            return cp(help_text)
        if a and a[0] == "usage":
            return cp("balance: 500 CU (stub)")
        if a and a[0] == "sessions":
            return cp("[]")
        return cp("")

    return stub


def _tmp_catalog(tmp_path, monkeypatch):
    # The measured-rate table is the catalog's only rate source:
    # cpu-highram carries its measured 0.26; every other shape is
    # absent, hence unconfirmed.
    var = tmp_path / "var"
    var.mkdir(parents=True)
    (var / "shape-rates.json").write_text(json.dumps({"cpu-highram": 0.26}))
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(var))


def test_specs_dynamic_catalog_covers_the_cli_space(tmp_path, monkeypatch):
    _tmp_catalog(tmp_path, monkeypatch)
    specs = {s["id"]: s for s in lease.specs(tmp_path, _help_stub())}
    # The catalog is the CLI space itself, keyed by shape; the
    # measured-rate table prices cpu-highram and nothing else here.
    assert specs["cpu-highram"]["label"] == "CPU High-RAM"
    assert specs["cpu-highram"]["rate_cu_per_hour"] == 0.26
    assert specs["t4"]["rate_cu_per_hour"] is None
    # Everything the CLI offers is served, unconfigured rates
    # unconfirmed.
    for pid in ("l4", "g4", "h100", "a100", "a100-highram", "t4-highram", "tpu-v5e1", "tpu-v6e1", "cpu"):
        assert pid in specs, pid
        assert specs[pid]["rate_cu_per_hour"] is None
    # L4 is on the CLI's single-shape list: no high-RAM variant exists.
    assert "l4-highram" not in specs
    assert specs["l4"]["gpu"] == "L4" and specs["l4"]["high_mem"] is False
    assert specs["a100-highram"]["gpu"] == "A100" and specs["a100-highram"]["high_mem"] is True
    assert specs["tpu-v6e1"]["tpu"] == "v6e1" and specs["tpu-v6e1"]["gpu"] is None


def test_specs_catalog_is_cached_across_calls(tmp_path, monkeypatch):
    _tmp_catalog(tmp_path, monkeypatch)
    calls = []
    base = _help_stub()

    def counting(*a, **k):
        if a[:2] == ("new", "--help"):
            calls.append(a)
        return base(*a, **k)

    lease.specs(tmp_path, counting)
    lease.specs(tmp_path, counting)
    assert len(calls) == 1


def test_state_surfaces_a_catalog_failure(tmp_path, monkeypatch):
    _tmp_catalog(tmp_path, monkeypatch)
    out = lease.state(_help_stub("garbage, no variants"), tmp_path)
    assert "catalog_error" in out
    assert {s["id"] for s in out["specs"]} == {"cpu", "cpu-highram"}
