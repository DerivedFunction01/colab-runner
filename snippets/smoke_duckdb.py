#!/usr/bin/env python3
"""DuckDB smoke: deterministic aggregate whose result must match locally.

Usage: smoke_duckdb.py [parquet_path]
Without a path, builds a 1M-row in-memory table from a seeded generator.
"""

import json
import sys


def main() -> int:
    import duckdb

    con = duckdb.connect()
    # Under `colab exec -f`, sys.argv carries the CLI's own flags (observed:
    # argv[1] == "-f"), so only a plausible parquet path counts as an argument.
    if len(sys.argv) > 1 and sys.argv[1].endswith(".parquet"):
        rows, total = con.execute(
            "SELECT count(*), sum(amount) FROM read_parquet(?)", [sys.argv[1]]
        ).fetchone()
    else:
        rows, total = con.execute(
            "SELECT count(*), sum(v) FROM (SELECT (i % 97) * 1.5 AS v FROM range(1000000) t(i))"
        ).fetchone()
    print(json.dumps({"rows": rows, "total": float(total), "duckdb": duckdb.__version__}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
