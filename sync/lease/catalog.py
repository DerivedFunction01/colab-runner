import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import foundation

def shape_id_for(gpu, tpu, high_mem) -> str:
    """The mechanical catalog id for a shape's fields — the same
    derivation the dynamic catalog always used (a GPU variant's
    lowercased name, a TPU's prefixed name, "cpu" for plain CPU;
    "-highram" appended for a high-memory variant)."""
    if gpu:
        return str(gpu).lower() + ("-highram" if high_mem else "")
    if tpu:
        return "tpu-" + str(tpu).lower() + ("-highram" if high_mem else "")
    return "cpu" + ("-highram" if high_mem else "")


# --- The dynamic shape catalog ------------------------------------------------
# Local why: the option set is parsed, never hand-listed, so a CLI
# format change surfaces as `catalog_error` instead of an invented
# or missing catalog; CPU shapes serve even when parsing fails.
# Mechanics: docs/lease-workflow.md.

CLI_CATALOG_TTL_S = 24 * 3600
SHAPE_TIMEOUT_S = 18000  # the configured accelerator shapes' timeout


def parse_cli_shapes(help_text: str) -> dict | None:
    """The accelerator space from `colab new --help`: {"gpu": [...],
    "tpu": [...], "no_high_mem": [...]}. The help is a wrapped,
    box-drawn table, so the text is flattened before matching. None
    when either variant list is absent — a format change must shrink
    the catalog loudly (catalog_error), never silently invent shapes."""
    flat = re.sub(r"\s+", " ", help_text.replace("│", " "))

    def variants(kind: str) -> list[str] | None:
        m = re.search(kind + r" accelerator variant\. Supported: ([^.]+)\.", flat)
        if not m:
            return None
        out = [v for v in re.split(r"[,\s]+", m.group(1)) if v]
        return out or None

    gpu, tpu = variants("GPU"), variants("TPU")
    if gpu is None or tpu is None:
        return None
    m = re.search(r"single shape \(([^)]+)\)", flat)
    no_high_mem = [v for v in re.split(r"[,\s]+", m.group(1)) if v] if m else []
    return {"gpu": gpu, "tpu": tpu, "no_high_mem": no_high_mem}


def _catalog_cache_path(repo: Path) -> Path:
    return foundation.var_dir(repo) / "cli-catalog.json"


def _cli_shapes(repo: Path, colab=None):
    """(shapes, error): the parsed CLI space, from the day-cache when
    fresh, else by running `colab new --help` through the injected
    callable. Without a callable only the cache can serve. A failed
    refresh falls back to any cache and reports the failure; with no
    cache at all the dynamic catalog is empty and the error stands."""
    cache = foundation.load_json(_catalog_cache_path(repo), None)
    cached = None
    if isinstance(cache, dict) and cache.get("gpu") and cache.get("tpu"):
        cached = {"gpu": cache["gpu"], "tpu": cache["tpu"], "no_high_mem": cache.get("no_high_mem", [])}
        try:
            age = (datetime.now(timezone.utc) - foundation.parse_dt(cache["fetched_at"])).total_seconds()
        except Exception:  # noqa: BLE001
            age = CLI_CATALOG_TTL_S + 1
        if age <= CLI_CATALOG_TTL_S:
            return cached, None
    if colab is None:
        return cached, None
    try:
        r = colab("new", "--help", timeout=60)
        shapes = parse_cli_shapes(r.stdout or "") if r.returncode == 0 else None
        if shapes is None:
            raise ValueError("unparseable `colab new --help` output")
    except Exception as exc:  # noqa: BLE001
        if cached is not None:
            return cached, f"cli catalog refresh failed ({exc}); serving the cached shape list"
        return None, f"cli catalog unavailable ({exc}); serving the CPU shapes only"
    try:
        p = _catalog_cache_path(repo)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), **shapes}, indent=2))
    except OSError:
        pass
    return shapes, None


def _shape_entries(shapes: dict | None) -> dict:
    """Catalog entries for every CLI shape: {id: {id, label, gpu,
    tpu, high_mem, timeout}} — identity, label, and shape fields
    only; the rate joins in catalog_shapes (it has another home).
    Ids and labels are mechanical. With no parsed CLI space, the
    two CPU shapes still serve — plain CPU and High-RAM CPU exist
    in every Colab shape space the CLI has ever printed."""
    out: dict = {}

    def add(label, gpu, tpu, high_mem):
        sid = shape_id_for(gpu, tpu, high_mem)
        if sid not in out:
            out[sid] = {"id": sid, "label": label, "gpu": gpu, "tpu": tpu, "high_mem": high_mem, "timeout": SHAPE_TIMEOUT_S}

    if shapes:
        for gpu in shapes["gpu"]:
            for hm in (False, True):
                if hm and gpu in shapes["no_high_mem"]:
                    continue
                add(gpu + (" High-RAM" if hm else ""), gpu, None, hm)
        for tpu in shapes["tpu"]:
            for hm in (False, True):
                if hm and tpu in shapes["no_high_mem"]:
                    continue
                add("TPU " + tpu + (" High-RAM" if hm else ""), None, tpu, hm)
    for hm in (False, True):
        add("CPU" + (" High-RAM" if hm else ""), None, None, hm)
    return out


# --- Per-shape measured rates ---------------------------------------------------
# Local why: an absent rate stays null (unconfirmed); no cost
# projection is fabricated for it.


def shape_rates_path(repo: Path) -> Path:
    return foundation.var_dir(repo) / "shape-rates.json"


def load_shape_rates(repo: Path) -> dict:
    raw = foundation.load_json(shape_rates_path(repo), {})
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def shape_rate(repo: Path, shape_id: str):
    """A shape's measured CU/hour rate, or None (unconfirmed):
    the measured-rate table first, then the config tier map for
    the CPU shapes."""
    table = load_shape_rates(repo)
    if shape_id in table:
        return table[shape_id]
    if shape_id in ("cpu", "cpu-highram"):
        tier = foundation.load_config(repo).get("rate_cu_per_hour")
        if isinstance(tier, dict):
            return tier.get("high_mem" if shape_id == "cpu-highram" else "standard")
    return None


def catalog_shapes(repo: Path, colab=None) -> dict:
    """The full shape catalog: every CLI shape with its measured
    rate joined (null = unconfirmed). Callers without a colab
    callable get whatever the day-cache holds, plus the CPU
    shapes, which always serve."""
    shapes, _err = _cli_shapes(repo, colab)
    entries = _shape_entries(shapes)
    for sid, entry in entries.items():
        entry["rate_cu_per_hour"] = shape_rate(repo, sid)
    return entries


def default_shape(repo: Path) -> str | None:
    """The config's default shape id (config/lease.json's
    `default_shape`) — the fallback a manifest or wrapper resolves
    when it names no shape. None when the key is absent: callers
    fail loudly rather than substituting a shape."""
    name = foundation.load_config(repo).get("default_shape")
    return name if isinstance(name, str) and name else None


def standing_caller(repo: Path) -> str:
    """The standing duty's caller: config/lease.json's
    standing.caller, defaulting to the machinery infra actor —
    the standing lease is identified by its caller (and its
    record's standing flag), never by a profile name."""
    raw = foundation.load_config(repo).get("standing", {}).get("caller")
    return raw if isinstance(raw, str) and raw else foundation.GATE_DEFAULT_INFRA_ACTOR


def standing_shape(repo: Path) -> str:
    """The standing lease's shape: config/lease.json's
    standing.shape when set, else the plain CPU shape."""
    raw = foundation.load_config(repo).get("standing", {}).get("shape")
    if isinstance(raw, str) and raw:
        return raw
    return "cpu"


def _session_shape_args(shape: dict) -> list[str]:
    return (["--high-mem"] if shape.get("high_mem") else []) + (["--gpu", shape["gpu"]] if shape.get("gpu") else []) + (["--tpu", shape["tpu"]] if shape.get("tpu") else [])
