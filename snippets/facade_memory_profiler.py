"""Scale-profile a built clone's XBRL facade on the leased VM (Step 1).

One file, two lives: launched by system python3 it re-execs itself under
the repo venv interpreter, then profiles in that dedicated process.
Follows the colab-run harness contract: progress to <out>/heartbeat.log,
completion is <out>/checkpoint.json with completed_units=1 plus a final
heartbeat line containing "complete".
Outputs: profile-report.json (all measurements), profile.log.
"""

from __future__ import annotations

import argparse
import gc
import os
import sys
import time
from pathlib import Path

def _resolve_profile_repo():
    """Resolve (repo_dir, venv_python) for the profiled clone.

    Primary source is the env build's remote registry
    (/content/env-registry.json): the repo id comes from $PROFILE_REPO_ID
    (or $COLAB_REPO_ID); $PROFILE_REPO, when set, is taken as a dest slug
    or a clone path whose final component is the dest. With no registry,
    $PROFILE_REPO as a path plus a conventional .venv is the fallback."""
    import json

    repo_id = os.environ.get("PROFILE_REPO_ID") or os.environ.get("COLAB_REPO_ID")
    legacy = os.environ.get("PROFILE_REPO")
    registry_path = Path("/content/env-registry.json")
    records = {}
    if registry_path.exists():
        try:
            records = json.loads(registry_path.read_text())
        except Exception:
            records = {}
    record = None
    if repo_id:
        record = next((r for r in records.values() if r.get("repo") == repo_id), None)
    elif legacy:
        dest = Path(legacy).name
        record = records.get(dest)
    elif len(records) == 1:
        record = next(iter(records.values()))
    if record:
        return Path("/content") / record["dest"], Path(record["venv_python"])
    if legacy:
        repo = Path(legacy)
        return repo, repo / ".venv" / "bin" / "python"
    raise SystemExit(
        "facade_memory_profiler: no built clone resolvable — set PROFILE_REPO_ID "
        "or PROFILE_REPO, or run the env build so /content/env-registry.json exists"
    )


REPO, _VENV_PY = _resolve_profile_repo()

if "--bootstrapped" not in sys.argv and _VENV_PY.exists():
    if Path(sys.executable).resolve() != _VENV_PY.resolve():
        os.execv(
            str(_VENV_PY),
            [str(_VENV_PY), str(Path(__file__).resolve()), "--bootstrapped", *sys.argv[1:]],
        )


def _rss_mb() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024.0
    return -1.0


# Allocation-site filename fragment -> component name, first match wins.
_COMPONENT_RULES: tuple[tuple[str, str], ...] = (
    ("apps/client/facts.py", "display_records_fact_set"),
    ("apps/client/kernel.py", "kernel_indexes_caches"),
    ("apps/client/financials.py", "financials_caches"),
    ("apps/client/statements.py", "statements_surface"),
    ("apps/client/rollforwards.py", "view_graphs"),
    ("apps/client/equity_rollforward.py", "view_graphs"),
    ("apps/client/views.py", "view_graphs"),
    ("engine/xbrl/solve/statement.py", "statement_graphs"),
    ("engine/xbrl/solve/composer.py", "statement_graphs"),
    ("engine/xbrl/solve/rollforward_graphs.py", "view_graphs"),
    ("engine/xbrl/schema/models.py", "fact_arc_objects"),
    ("engine/xbrl/storage/storage.py", "loader_storage"),
    ("tools/facade_battery.py", "loader_frames"),
    ("pyarrow", "loader_arrow_untracked_note"),
    ("pandas", "pandas_frames"),
    ("engine/xbrl/taxonomy/", "taxonomy"),
    ("engine/xbrl/solve/tokenizer.py", "tokenizer"),
    ("engine/xbrl/normalize/", "tokenizer"),
    ("engine/xbrl/evidence/", "solver_engine"),
    ("engine/xbrl/solve/", "solver_engine"),
)

_SCALARS = (
    "revenue", "gross_profit", "operating_income", "net_income",
    "total_assets", "total_liabilities", "total_equity",
    "operating_cash_flow", "rpo", "capex", "free_cash_flow",
    "uncommenced_leases",
)
_SERIES_BASES = (
    "revenue", "gross_profit", "operating_income", "net_income",
    "total_assets", "total_liabilities", "total_equity",
    "operating_cash_flow", "rpo", "capex",
)
_VIEWS = (
    "derivatives", "debt", "cash_rollforward", "fair_value_hierarchy",
    "pension_funded_status", "equity_rollforward", "segment_breakdown",
)


def _component_of(filename: str) -> str:
    for fragment, name in _COMPONENT_RULES:
        if fragment in filename:
            return name
    return "other"


def main() -> int:
    argv = [a for a in sys.argv[1:] if a != "--bootstrapped"]
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--stream-iters", type=int, default=300)
    parser.add_argument("--folders", nargs="*", default=None,
                        help="Subset of sp100 folders for phases A/B/D.")
    args = parser.parse_args(argv)
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    heartbeat = out / "heartbeat.log"
    log_path = out / "profile.log"

    def beat(message: str) -> None:
        with heartbeat.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {message}\n")

    def log(message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {message}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    sys.path.insert(0, str(REPO))
    os.chdir(REPO)

    import json
    import traceback
    import tracemalloc

    report: dict = {"substrate": {}, "phases": {}}
    try:
        import platform

        import pyarrow.parquet as pq

        from workload_sec.apps.client.xbrl import Xbrl
        from workload_sec.domain.identity import AccessionNumber, Cik
        from workload_sec.engine.xbrl.schema.models import FilingXbrl, LinkbaseBundle
        from workload_sec.engine.xbrl.storage.storage import (
            calculation_arcs_from_arrow_table,
            presentation_arcs_from_arrow_table,
        )
        from workload_sec.engine.xbrl.taxonomy.domain import load_domain_taxonomy
        from workload_sec.foundation.runtime.memory import reclaim
        from workload_sec.tools.facade_battery import cohort_folders, load_bundle_facts

        mem_total_kb = 0
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                mem_total_kb = int(line.split()[1])
        report["substrate"] = {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "mem_total_mb": round(mem_total_kb / 1024.0, 1),
            "repo": str(REPO),
        }
        beat("imports ok; loading taxonomy")
        t0 = time.perf_counter()
        taxonomy = load_domain_taxonomy()
        report["baseline"] = {
            "taxonomy_load_s": round(time.perf_counter() - t0, 3),
            "rss_mb": round(_rss_mb(), 1),
        }
        log(f"baseline rss={report['baseline']['rss_mb']} MB "
            f"taxonomy={report['baseline']['taxonomy_load_s']}s")

        fixtures = REPO / "tests" / "fixtures" / "xbrl"

        def bundle_path(cohort: str, folder: str) -> Path:
            return fixtures / cohort / folder

        def load_parts(cohort: str, folder: str):
            bundle = bundle_path(cohort, folder)
            facts, accession = load_bundle_facts(bundle)
            calc = calculation_arcs_from_arrow_table(
                pq.read_table(bundle / "calculation_arcs.parquet"))
            pres = presentation_arcs_from_arrow_table(
                pq.read_table(bundle / "presentation_arcs.parquet"))
            return facts, accession, calc, pres

        def build(facts, accession, calc, pres) -> Xbrl:
            filing = FilingXbrl(
                accession=AccessionNumber.from_any(accession),
                cik=Cik(0),
                facts=facts,
                linkbases=LinkbaseBundle(
                    calculation_arcs=calc, presentation_arcs=pres),
            )
            return Xbrl(filing, taxonomy)

        def run_queries(x: Xbrl) -> None:
            for term in ("revenue", "net income", "total assets"):
                try:
                    x.query(term)
                except Exception:
                    pass

        def run_compose(x: Xbrl) -> None:
            for name in ("income_statement", "balance_sheet", "cash_flow_statement"):
                try:
                    getattr(x.statements, name)()
                except Exception:
                    pass

        def run_search(x: Xbrl) -> None:
            try:
                x.search("revenue")
            except Exception:
                pass

        def run_sweep(x: Xbrl) -> None:
            fin = x.financials
            for name in _SCALARS:
                try:
                    getattr(fin, name)
                except Exception:
                    pass
            for base in _SERIES_BASES:
                for suffix in ("_series", "_yoy"):
                    try:
                        getattr(fin, base + suffix)
                    except Exception:
                        pass
            try:
                fin.metrics()
            except Exception:
                pass
            for name in _VIEWS:
                try:
                    getattr(fin, name)()
                except Exception:
                    pass

        def stage(fn) -> tuple[float, str | None]:
            start = time.perf_counter()
            err = None
            try:
                fn()
            except Exception as exc:  # per-stage errors are data
                err = f"{type(exc).__name__}: {exc}"
            return time.perf_counter() - start, err

        # ---- Phase A: per-stage timing + RSS over the SP100 corpus ----
        beat("phase A: sp100 per-stage profile")
        sp100 = cohort_folders("sp100", args.folders)
        phase_a: list[dict] = []
        for idx, folder in enumerate(sp100):
            rec: dict = {"folder": folder, "errors": {}}
            rss0 = _rss_mb()
            parts: dict = {}
            t, err = stage(lambda: parts.update(
                zip(("facts", "accession", "calc", "pres"),
                    load_parts("sp100", folder))))
            rec["t_load_s"] = round(t, 4)
            if err:
                rec["errors"]["load"] = err
                phase_a.append(rec)
                continue
            rec["facts"] = len(parts["facts"])
            rec["rss_after_load_mb"] = round(_rss_mb(), 1)
            holder: dict = {}
            t, err = stage(lambda: holder.update(
                x=build(parts["facts"], parts["accession"],
                        parts["calc"], parts["pres"])))
            rec["t_build_s"] = round(t, 4)
            if err:
                rec["errors"]["build"] = err
                phase_a.append(rec)
                continue
            x = holder["x"]
            rec["rss_after_build_mb"] = round(_rss_mb(), 1)
            for label, fn in (("query", run_queries), ("compose", run_compose),
                              ("search", run_search), ("sweep", run_sweep)):
                t, err = stage(lambda fn=fn: fn(x))
                rec[f"t_{label}_s"] = round(t, 4)
                if err:
                    rec["errors"][label] = err
            rec["rss_after_sweep_mb"] = round(_rss_mb(), 1)
            del x, holder, parts
            gc.collect()
            rec["rss_after_drop_mb"] = round(_rss_mb(), 1)
            rec["rss_before_mb"] = round(rss0, 1)
            phase_a.append(rec)
            if (idx + 1) % 10 == 0:
                beat(f"phase A {idx + 1}/{len(sp100)} rss={_rss_mb():.0f} MB")
        report["phases"]["A_sp100"] = phase_a
        log(f"phase A done: {len(phase_a)} filings")

        # ---- Phase B: staged tracemalloc attribution on 3 representatives ----
        beat("phase B: tracemalloc attribution")
        ok_a = [r for r in phase_a if "facts" in r]
        ok_a.sort(key=lambda r: r["facts"])
        reps = []
        if ok_a:
            reps = [ok_a[0]["folder"],
                    ok_a[len(ok_a) // 2]["folder"],
                    ok_a[-1]["folder"]]
        phase_b: dict = {}
        tracemalloc.start(10)
        for folder in reps:
            snaps = []
            parts_b: dict = {}
            holder_b: dict = {}
            stage_fns = [
                ("load", lambda: parts_b.update(
                    zip(("facts", "accession", "calc", "pres"),
                        load_parts("sp100", folder)))),
                ("build", lambda: holder_b.update(
                    x=build(parts_b["facts"], parts_b["accession"],
                            parts_b["calc"], parts_b["pres"]))),
                ("query", lambda: run_queries(holder_b["x"])),
                ("compose", lambda: run_compose(holder_b["x"])),
                ("search", lambda: run_search(holder_b["x"])),
                ("sweep", lambda: run_sweep(holder_b["x"])),
            ]
            snaps.append(tracemalloc.take_snapshot())
            stage_records = []
            for label, fn in stage_fns:
                fn()
                snap = tracemalloc.take_snapshot()
                diff = snap.compare_to(snaps[-1], "lineno")
                by_component: dict[str, float] = {}
                sites: list[tuple[float, str]] = []
                for stat in diff:
                    if stat.size_diff <= 0:
                        continue
                    frame = stat.traceback[-1]
                    comp = _component_of(frame.filename)
                    by_component[comp] = by_component.get(comp, 0.0) + stat.size_diff
                    sites.append((stat.size_diff,
                                  f"{frame.filename}:{frame.lineno}"))
                sites.sort(reverse=True)
                stage_records.append({
                    "stage": label,
                    "total_kb": round(sum(by_component.values()) / 1024.0, 1),
                    "by_component_kb": {
                        k: round(v / 1024.0, 1)
                        for k, v in sorted(by_component.items(),
                                           key=lambda kv: -kv[1])
                    },
                    "top_sites": [
                        {"kb": round(kb / 1024.0, 1), "site": site}
                        for kb, site in sites[:8]
                    ],
                })
                snaps.append(snap)
            phase_b[folder] = {
                "facts": len(parts_b["facts"]),
                "stages": stage_records,
            }
            del holder_b, parts_b, snaps
            gc.collect()
            beat(f"phase B {folder} done")
        tracemalloc.stop()
        report["phases"]["B_attribution"] = phase_b
        report["tracemalloc_note"] = (
            "tracemalloc tracks Python-object allocations only; pyarrow/C "
            "buffers are invisible to it and show up only in RSS."
        )
        log(f"phase B done: reps={reps}")

        # ---- Phase D: hold all SP100 facades ----
        beat("phase D: hold-N over sp100")
        gc.collect()
        reclaim()
        rss_pre = _rss_mb()
        held: list = []
        total_facts = 0
        hold_errors: dict[str, str] = {}
        for folder in sp100:
            try:
                facts, accession, calc, pres = load_parts("sp100", folder)
                held.append(build(facts, accession, calc, pres))
                total_facts += len(facts)
            except Exception as exc:
                hold_errors[folder] = f"{type(exc).__name__}: {exc}"
        rss_held = _rss_mb()
        report["phases"]["D_hold"] = {
            "facades": len(held),
            "total_facts": total_facts,
            "rss_pre_mb": round(rss_pre, 1),
            "rss_held_mb": round(rss_held, 1),
            "retained_mb": round(rss_held - rss_pre, 1),
            "kb_per_fact": (round((rss_held - rss_pre) * 1024.0 / total_facts, 2)
                            if total_facts else None),
            "errors": hold_errors,
        }
        del held
        gc.collect()
        reclaim()
        report["phases"]["D_hold"]["rss_after_release_mb"] = round(_rss_mb(), 1)
        log(f"phase D done: {report['phases']['D_hold']}")

        # ---- Phase C: streaming steady state over repeated lifecycles ----
        beat(f"phase C: streaming x{args.stream_iters}")
        cycle = ([("sp100", f) for f in cohort_folders("sp100")]
                 + [("seed", f) for f in cohort_folders("seed")])
        samples: list[dict] = []
        sample_at = {1, 2, 3, 5, 10, 20, 30, 50, 75, 100, 150, 200, 250,
                     args.stream_iters}
        t_c = time.perf_counter()
        for i in range(1, args.stream_iters + 1):
            cohort, folder = cycle[(i - 1) % len(cycle)]
            try:
                facts, accession, calc, pres = load_parts(cohort, folder)
                x = build(facts, accession, calc, pres)
                run_queries(x)
                try:
                    x.statements.income_statement()
                except Exception:
                    pass
                del x, facts, calc, pres
            except Exception:
                pass
            if i % 25 == 0:
                gc.collect()
                reclaim()
            if i in sample_at:
                samples.append({"i": i, "rss_mb": round(_rss_mb(), 1)})
                beat(f"phase C i={i} rss={_rss_mb():.0f} MB")
        report["phases"]["C_streaming"] = {
            "iters": args.stream_iters,
            "seconds": round(time.perf_counter() - t_c, 1),
            "samples": samples,
        }
        log(f"phase C done in {report['phases']['C_streaming']['seconds']}s")

        report["final_rss_mb"] = round(_rss_mb(), 1)
        rc = 0
    except Exception:
        log("profiler failure:\n" + traceback.format_exc())
        rc = 1

    (out / "profile-report.json").write_text(
        json.dumps(report, indent=1) + "\n", encoding="utf-8")
    (out / "checkpoint.json").write_text(
        json.dumps({"completed_units": 1, "rc": rc}) + "\n", encoding="utf-8")
    beat(f"profiler exited rc={rc}")
    beat("memory profile complete")
    return rc


if __name__ == "__main__":
    sys.exit(main())
