#!/usr/bin/env python3
"""BERT batch ramp: forward passes at doubling batch sizes until OOM.

Records per-batch peak RSS so a job manifest's memory model can be checked
against a measured ceiling. If torch/transformers are unavailable, records
that verdict and exits 0 — absence is a finding, not a crash.
"""

import json
import os
import resource
import sys
import time
from pathlib import Path

try:
    _here = str(Path(__file__).parent)
except NameError:  # under `colab exec`, __file__ is undefined (IPython cell)
    _here = os.getcwd()
sys.path.insert(0, _here)
from _remote_guard import require_colab


def rss() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def main() -> int:
    forced_local = require_colab("bert_batch_ramp")
    out = {"model": "bert-base-uncased", "seq_len": 512, "batches": []}
    max_batch = 8 if forced_local else 4096
    try:
        import torch
        from transformers import AutoModel, AutoTokenizer
    except ImportError as e:
        out["verdict"] = f"unavailable: {e.name} not installed on this runtime"
        print(json.dumps(out, indent=2))
        return 0

    t0 = time.perf_counter()
    tok = AutoTokenizer.from_pretrained(out["model"])
    model = AutoModel.from_pretrained(out["model"])
    model.eval()
    out["download_seconds"] = round(time.perf_counter() - t0, 2)
    cache = os.path.expanduser("~/.cache/huggingface")
    out["hf_cache_bytes"] = sum(p.stat().st_size for p in __import__("pathlib").Path(cache).rglob("*") if p.is_file()) if os.path.exists(cache) else 0

    batch = 1
    while batch <= max_batch:
        try:
            ids = tok(["the quarterly filing reported revenue growth"] * batch, return_tensors="pt", padding="max_length", max_length=out["seq_len"], truncation=True)["input_ids"]
            with torch.no_grad():
                model(ids)
            out["batches"].append({"batch": batch, "ok": True, "peak_rss_bytes": rss()})
            batch *= 2
        except (RuntimeError, MemoryError) as e:
            out["batches"].append({"batch": batch, "ok": False, "error": type(e).__name__, "peak_rss_bytes": rss()})
            out["ceiling_batch"] = batch // 2
            break
    else:
        out["ceiling_batch"] = max_batch
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
