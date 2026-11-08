"""Chunked transfer unit tests against a fake colab CLI (no network).

The fake keeps a dict as the VM's /content and dispatches exec calls
on the transfer snippets' marker comments. Pins the 2026-10-31
reliability contract: a part that fails is retried, a part already on
the far side with a matching SHA-256 is not moved again (resume), a
same-size corrupt part is detected by hash and re-moved, and a
transfer that exhausts its attempts reports the failed stage so a
later re-run resumes instead of restarting.

The bin/colab-xfer section loads the verb against the same fake and
pins the E7 contract: the move lands in parts, the transfer record
at results/xfers/<id>.json tracks part progress as it happens and
the terminal state at the end, and a failed move re-invoked with
the same id resumes from the verified parts.
"""

import hashlib
import importlib.util
import json
import re
import subprocess
from importlib.machinery import SourceFileLoader
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
            out_name = re.search(r"open\(os\.path\.join\(BASE, '([^']+)'\), 'wb'\)", code).group(1)
            blob = b"".join(self.remote[p] for p in parts)
            self.remote[out_name] = blob
            return ok({"sha256": sha(blob)})
        if "# transfer:split" in code:
            remote = json.loads(re.search(r'REMOTE = (".*?")', code).group(1))
            blob = self.remote[remote]
            parts = []
            ch = int(re.search(r"CH = (\d+)", code).group(1))
            for i in range(0, len(blob), ch):
                c = blob[i:i + ch]
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


def test_download_exhaustion_reports_stage_then_rerun_resumes(tmp_path, monkeypatch):
    fake, local = harness(tmp_path, monkeypatch)
    fake.remote["big.bin"] = CONTENT
    fake.fail_downloads["big.bin.dl-002"] = 99  # part 2 never lands
    dest = tmp_path / "down" / "big.bin"
    r = xfer.chunked_download("s", "big.bin", dest)
    assert not r["ok"] and r["stage"] == "download big.bin.dl-002" and r["attempts"] == xfer.PART_ATTEMPTS
    fake.fail_downloads.clear()
    calls_before = len(fake.download_calls)
    r2 = xfer.chunked_download("s", "big.bin", dest)
    # Parts 0-1 landed verified before the failure; part 3 was never attempted.
    assert r2["ok"] and r2["resumed_parts"] == 2 and r2["downloaded_parts"] == 2
    assert len(fake.download_calls) - calls_before == 2  # only the missing parts moved
    assert dest.read_bytes() == CONTENT


# --- bin/colab-xfer: the verb over the library ------------------------------

vspec = importlib.util.spec_from_loader(
    "colab_xfer", SourceFileLoader("colab_xfer", str(REPO / "bin" / "colab-xfer")))
verb = importlib.util.module_from_spec(vspec)
vspec.loader.exec_module(verb)


def verb_harness(tmp_path, monkeypatch):
    """The verb's own transfer module wired to the fake world, its
    records rooted in tmp, and no ambient caller claim — the claim
    the environment happens to carry is not the verb's to inherit
    in a test."""
    fake = FakeColab()
    t = verb.transfer
    monkeypatch.setattr(t, "REPO", tmp_path)
    monkeypatch.setattr(t, "CHUNK", 64)
    monkeypatch.setattr(t, "PART_BACKOFF_S", 0)
    monkeypatch.setattr(t, "colab", fake)
    monkeypatch.setattr(verb, "XFERS", tmp_path / "xfers")
    monkeypatch.delenv("COLAB_CALLER", raising=False)
    local = tmp_path / "big.bin"
    local.write_bytes(CONTENT)
    return fake, local


def read_xfer_record(tmp_path, xfer_id):
    return json.loads((tmp_path / "xfers" / f"{xfer_id}.json").read_text())


def test_xfer_up_completes_in_parts_and_records_terminal_state(tmp_path, monkeypatch, capsys):
    fake, local = verb_harness(tmp_path, monkeypatch)
    rc = verb.main(["up", "-s", "s", str(local), "/content/big.bin", "--id", "up-1"])
    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    rec = read_xfer_record(tmp_path, "up-1")
    assert printed == rec  # what the caller sees is the stored record
    assert rec["state"] == "complete" and rec["direction"] == "up"
    assert rec["remote_name"] == "big.bin"  # the /content/ prefix was stripped
    assert rec["parts"] == 4 and rec["parts_done"] == 4
    assert rec["resumed_parts"] == 0 and rec["moved_parts"] == 4
    assert rec["sha256"] == sha(CONTENT) and rec["remote_sha256"] == sha(CONTENT)
    assert rec["bytes"] == len(CONTENT) and rec["bytes"] > verb.transfer.CHUNK
    assert rec["started_at"] and rec["finished_at"] and rec["error"] is None
    assert rec["caller"] is None
    assert fake.remote["big.bin"] == CONTENT


def test_xfer_record_tracks_part_progress_as_it_happens(tmp_path, monkeypatch, capsys):
    fake, local = verb_harness(tmp_path, monkeypatch)
    record_path = tmp_path / "xfers" / "up-2.json"
    seen = []
    real_colab = verb.transfer.colab

    def spying_colab(*a, **kw):
        if a and a[0] == "upload" and record_path.exists():
            seen.append(json.loads(record_path.read_text())["parts_done"])
        return real_colab(*a, **kw)

    monkeypatch.setattr(verb.transfer, "colab", spying_colab)
    rc = verb.main(["up", "-s", "s", str(local), "big.bin", "--id", "up-2"])
    capsys.readouterr()
    assert rc == 0
    # Each upload after the first could already read a record whose
    # parts_done counted the parts landed before it.
    assert seen == [0, 1, 2, 3]


def test_xfer_up_exhaustion_records_the_failed_stage(tmp_path, monkeypatch, capsys):
    fake, local = verb_harness(tmp_path, monkeypatch)
    fake.fail_uploads["big.bin.part-001"] = 99
    rc = verb.main(["up", "-s", "s", str(local), "big.bin", "--id", "up-3"])
    capsys.readouterr()
    assert rc == 1
    rec = read_xfer_record(tmp_path, "up-3")
    assert rec["state"] == "failed" and rec["error"]["stage"] == "upload part 1"
    assert rec["parts_done"] == 1 and rec["moved_parts"] == 1
    assert rec["finished_at"]


def test_xfer_down_killed_part_then_rerun_resumes_verified_parts(tmp_path, monkeypatch, capsys):
    fake, local = verb_harness(tmp_path, monkeypatch)
    fake.remote["big.bin"] = CONTENT
    fake.fail_downloads["big.bin.dl-002"] = 99  # the run dies on part 2
    dest = tmp_path / "down" / "big.bin"
    rc = verb.main(["down", "-s", "s", "big.bin", str(dest), "--id", "down-1"])
    capsys.readouterr()
    assert rc == 1
    rec = read_xfer_record(tmp_path, "down-1")
    assert rec["state"] == "failed" and rec["error"]["stage"] == "download big.bin.dl-002"
    assert rec["parts_done"] == 2  # parts 0-1 landed verified before the failure
    fake.fail_downloads.clear()
    calls_before = len(fake.download_calls)
    rc = verb.main(["down", "-s", "s", "big.bin", str(dest), "--id", "down-1"])
    capsys.readouterr()
    assert rc == 0
    assert len(fake.download_calls) - calls_before == 2  # only parts 2-3 re-moved
    rec = read_xfer_record(tmp_path, "down-1")
    assert rec["state"] == "complete" and rec["error"] is None
    assert rec["resumed_parts"] == 2 and rec["moved_parts"] == 2
    assert dest.read_bytes() == CONTENT


def test_xfer_status_reads_the_record_and_needs_no_session(tmp_path, monkeypatch, capsys):
    fake, local = verb_harness(tmp_path, monkeypatch)
    assert verb.main(["up", "-s", "s", str(local), "big.bin", "--id", "up-4"]) == 0
    capsys.readouterr()
    assert verb.main(["status", "up-4"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["xfer_id"] == "up-4" and out["state"] == "complete"
    assert verb.main(["status", "no-such-id"]) == 2


def test_xfer_refuses_a_remote_path_that_is_not_a_bare_name(tmp_path, monkeypatch, capsys):
    fake, local = verb_harness(tmp_path, monkeypatch)
    rc = verb.main(["up", "-s", "s", str(local), "sub/dir.bin", "--id", "up-5"])
    capsys.readouterr()
    assert rc == 2
    assert not (tmp_path / "xfers" / "up-5.json").exists()  # refused before any record
    assert fake.upload_calls == []
