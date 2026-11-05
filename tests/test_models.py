"""Model catalogue, selection and verified install, in a throwaway home. Run: python tests/test_models.py"""
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

home = Path(tempfile.mkdtemp())
os.environ["VOICELY_HOME"] = str(home)
from voicely import engine  # noqa: E402  (reads VOICELY_HOME at import)

try:
    for model_id, m in engine.CATALOG.items():
        assert {"name", "size", "hint", "dir"} <= m.keys(), model_id
        assert ("url" in m) != ("base" in m and "files" in m), model_id  # archive, or pinned files with hashes
    assert engine.DEFAULT_MODEL in engine.CATALOG

    assert engine.selected_model() == engine.DEFAULT_MODEL  # no config yet
    engine.CONFIG.write_text(json.dumps({"model": "gigaam-v3-ru"}), "utf-8")
    assert engine.selected_model() == "gigaam-v3-ru"
    engine.CONFIG.write_text(json.dumps({"model": "removed-model"}), "utf-8")
    assert engine.selected_model() == engine.DEFAULT_MODEL  # a stale choice must not break startup
    engine.CONFIG.write_text("not json", "utf-8")
    assert engine.selected_model() == engine.DEFAULT_MODEL

    engine.MODELS.mkdir()
    source = home / "server"
    source.mkdir()
    (source / "encoder.int8.onnx").write_bytes(b"weights")
    base = source.as_uri() + "/"
    good = hashlib.sha256(b"weights").hexdigest()
    target = engine.MODELS / "pinned"

    try:
        engine._install_files(target, base, {"encoder.int8.onnx": "0" * 64}, lambda _: None)
        raise SystemExit("a tampered file was accepted")
    except RuntimeError as e:
        assert "integrity" in str(e)
    assert not target.exists() and not list(engine.MODELS.glob(".*"))  # nothing left behind to be loaded

    engine._install_files(target, base, {"encoder.int8.onnx": good}, lambda _: None)
    assert (target / "encoder.int8.onnx").read_bytes() == b"weights"
    print("ok")
finally:
    shutil.rmtree(home, ignore_errors=True)
