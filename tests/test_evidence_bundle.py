"""B3 bundle 的 RED→GREEN：清单必须能抓到篡改与缺件。"""
import hashlib
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import service.make_evidence_bundle as mb  # noqa: E402


def _setup(tmp_path, monkeypatch):
    d = tmp_path / "evidence"
    d.mkdir()
    monkeypatch.setattr(mb, "_BUNDLE", str(d))
    f = d / "a.json"
    f.write_text('{"x": 1}', encoding="utf-8")
    text = mb.make_manifest(str(d), [("a.json", str(f))])
    (d / "MANIFEST.sha256").write_text(text, encoding="utf-8")
    return d, f, text


def test_green_clean_bundle_verifies(tmp_path, monkeypatch):
    d, _f, _text = _setup(tmp_path, monkeypatch)
    r = mb.verify()
    assert r["ok"] is True
    assert r["checked"] == 1
    assert r["bad"] == []


def test_red_tamper_detected(tmp_path, monkeypatch):
    d, f, _text = _setup(tmp_path, monkeypatch)
    f.write_text('{"x": 2}', encoding="utf-8")  # 注入违规：改一个字节
    r = mb.verify()
    assert r["ok"] is False
    assert any(x.startswith("hash_mismatch:a.json") for x in r["bad"])


def test_red_missing_file_detected(tmp_path, monkeypatch):
    d, f, _text = _setup(tmp_path, monkeypatch)
    os.remove(f)
    r = mb.verify()
    assert r["ok"] is False
    assert "missing:a.json" in r["bad"]


def test_manifest_is_sha256sum_compatible(tmp_path, monkeypatch):
    d, _f, text = _setup(tmp_path, monkeypatch)
    want = hashlib.sha256(b'{"x": 1}').hexdigest()
    assert text.startswith(want + "  a.json")
    p = subprocess.run(["sha256sum", "-c", "MANIFEST.sha256"], cwd=str(d),
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "a.json: OK" in p.stdout
