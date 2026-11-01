"""Chunked transfer unit tests against a fake colab CLI (no network).

The fake keeps a dict as the VM's /content and dispatches exec calls
on the transfer snippets' marker comments. Pins the 2026-10-31
reliability contract: a part that fails is retried, a part already on
the far side with a matching SHA-256 is not moved again (resume), a
same-size corrupt part is detected by hash and re-moved, and a
transfer that exhausts its attempts reports the failed stage so a
later re-run resumes instead of restarting.
"""

import hashlib
import importlib.util
import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("transfer", REPO / "sync" / "transfer.py")
xfer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(xfer)

CONTENT = bytes(range(200))  # 4 parts at the test chunk size of 64


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class FakeColab:
    def __init__(self):
        self.remote = {}          # /content: name -> bytes
        self.fail_uploads = {}    # part name -> failures left to inject
        self.fail_downloads = {}
        self.corrupt_downloads = {}  # part name -> corrupt deliveries left
        self.upload_calls = []
        self.download_calls = []

    def __call__(self, *args, timeout=3600):
        op = args[0]
        if op == "upload":
            local, remotepath = Path(args[3]), args[4]
            name = remotepath.split("/")[-1]
            self.upload_calls.append(name)
            if self.fail_uploads.get(name, 0) > 0:
                self.fail_uploads[name] -= 1
                return subprocess.CompletedProcess(args, 1, "", "injected upload failure")
            self.remote[name] = local.read_bytes()
            return subprocess.CompletedProcess(args, 0, "", "")
        if op == "download":
            remotepath, local = args[3], Path(args[4])
            name = remotepath.split("/")[-1]
            self.download_calls.append(name)
            if self.fail_downloads.get(name, 0) > 0:
                self.fail_downloads[name] -= 1
                return subprocess.CompletedProcess(args, 1, "", "injected download failure")
            data = self.remote[name]
            if self.corrupt_downloads.get(name, 0) > 0:
                self.corrupt_downloads[name] -= 1
                data = bytes([data[0] ^ 0xFF]) + data[1:]
            local.write_bytes(data)
            return subprocess.CompletedProcess(args, 0, "", "")
        if op == "exec":
            code = Path(args[args.index("-f") + 1]).read_text()
            return self._exec(code)
        raise AssertionError(f"unexpected colab op {op}")

    def _exec(self, code):
        def ok(payload):
            return subprocess.CompletedProcess([], 0, json.dumps(payload), "")

        if "# transfer:part-hashes" in code:
            names = json.loads(re.search(r"NAMES = (\[.*\])", code).group(1))
            return ok({n: sha(self.remote[n]) for n in names if n in self.remote})
        if "# transfer:reassemble" in code:
            parts = json.loads(re.search(r"parts = (\[.*\])", code).group(1))
            out_name = re.search(r"open\('/content/([^']+)', 'wb'\)", code).group(1)
            blob = b"".join(self.remote[p] for p in parts)
            self.remote[out_name] = blob
            return ok({"sha256": sha(blob)})
        if "# transfer:split" in code:
            remote = json.loads(re.search(r'REMOTE = (".*?")', code).group(1))
            blob = self.remote[remote]
            parts = []
            for i in range(0, len(blob), xfer.CHUNK):
                c = blob[i:i + xfer.CHUNK]
                name = f"{remote}.dl-{len(parts):03d}"
                self.remote[name] = c
                parts.append({"name": name, "size": len(c), "sha256": sha(c)})
            return ok({"size": len(blob), "sha256": sha(blob), "parts": parts})
        raise AssertionError("unmarked exec snippet")


def harness(tmp_path, monkeypatch):
    fake = FakeColab()
    monkeypatch.setattr(xfer, "REPO", tmp_path)
    monkeypatch.setattr(xfer, "CHUNK", 64)
    monkeypatch.setattr(xfer, "PART_BACKOFF_S", 0)
    monkeypatch.setattr(xfer, "colab", fake)
    local = tmp_path / "big.bin"
    local.write_bytes(CONTENT)
    return fake, local


def test_upload_clean_multipart(tmp_path, monkeypatch):
    fake, local = harness(tmp_path, monkeypatch)
    r = xfer.chunked_upload("s", local, "big.bin")
    assert r["ok"] and r["parts"] == 4
    assert r["uploaded_parts"] == 4 and r["resumed_parts"] == 0
    assert fake.remote["big.bin"] == CONTENT


def test_upload_retries_a_dropped_part(tmp_path, monkeypatch):
    fake, local = harness(tmp_path, monkeypatch)
    fake.fail_uploads["big.bin.part-001"] = 2  # attempts 1-2 drop, 3rd lands
    r = xfer.chunked_upload("s", local, "big.bin")
    assert r["ok"] and r["retried_parts"] == 1
    assert fake.remote["big.bin"] == CONTENT


def test_upload_exhaustion_reports_stage_then_rerun_resumes(tmp_path, monkeypatch):
    fake, local = harness(tmp_path, monkeypatch)
    fake.fail_uploads["big.bin.part-002"] = 99
    r = xfer.chunked_upload("s", local, "big.bin")
    assert not r["ok"] and r["stage"] == "upload part 2" and r["attempts"] == xfer.PART_ATTEMPTS
    fake.fail_uploads.clear()
    calls_before = len(fake.upload_calls)
    r2 = xfer.chunked_upload("s", local, "big.bin")
    # Parts 0-1 landed before the failure; part 3 was never attempted.
    assert r2["ok"] and r2["resumed_parts"] == 2 and r2["uploaded_parts"] == 2
    assert len(fake.upload_calls) - calls_before == 2  # only the missing parts moved


def test_upload_resume_reuploads_a_corrupt_remote_part(tmp_path, monkeypatch):
    fake, local = harness(tmp_path, monkeypatch)
    assert xfer.chunked_upload("s", local, "big.bin")["ok"]
    good = fake.remote["big.bin.part-001"]
    fake.remote["big.bin.part-001"] = bytes([good[0] ^ 0xFF]) + good[1:]  # same size, wrong bytes
    r = xfer.chunked_upload("s", local, "big.bin")
    assert r["ok"] and r["resumed_parts"] == 3 and r["uploaded_parts"] == 1
    assert fake.remote["big.bin"] == CONTENT


def test_download_clean_and_hash_verified(tmp_path, monkeypatch):
    fake, local = harness(tmp_path, monkeypatch)
    fake.remote["big.bin"] = CONTENT
    dest = tmp_path / "down" / "big.bin"
    r = xfer.chunked_download("s", "big.bin", dest)
    assert r["ok"] and r["bytes"] == len(CONTENT) and r["sha256"] == sha(CONTENT)
    assert dest.read_bytes() == CONTENT


def test_download_retries_and_rejects_corrupt_delivery(tmp_path, monkeypatch):
    fake, local = harness(tmp_path, monkeypatch)
    fake.remote["big.bin"] = CONTENT
    fake.fail_downloads["big.bin.dl-000"] = 1
    fake.corrupt_downloads["big.bin.dl-002"] = 1  # rc 0 but wrong bytes once
    dest = tmp_path / "down" / "big.bin"
    r = xfer.chunked_download("s", "big.bin", dest)
    assert r["ok"] and r["retried_parts"] == 2
    assert dest.read_bytes() == CONTENT


def test_download_resume_skips_verified_local_parts(tmp_path, monkeypatch):
    fake, local = harness(tmp_path, monkeypatch)
    fake.remote["big.bin"] = CONTENT
    dest = tmp_path / "down" / "big.bin"
    assert xfer.chunked_download("s", "big.bin", dest)["ok"]
    calls_before = len(fake.download_calls)
    r = xfer.chunked_download("s", "big.bin", dest)
    assert r["ok"] and r["resumed_parts"] == 4 and r["downloaded_parts"] == 0
    assert len(fake.download_calls) == calls_before
