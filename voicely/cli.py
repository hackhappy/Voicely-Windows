"""`voicely` — headless CLI and stdio MCP server. Data goes to stdout, progress to stderr."""
import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from typing import Literal

from . import engine, store
from .engine import log

_engine = engine.Engine()
Kind = Literal["dictations", "calls", "files"]


def transcribe_path(path, diarize=False):
    """File -> labelled segments. Shared by the CLI, the MCP server and the tray app."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    log(f"Decoding {path.name} ...")
    audio = engine.load_audio(path)
    log(f"Transcribing {len(audio) / engine.SR:.0f} s of audio ...")
    segments = _engine.transcribe(audio)
    if diarize and segments:
        log("Identifying speakers ...")
        engine.label_speakers(segments, _engine.diarize(audio))
    return segments


def model_name():
    return engine.CATALOG[_engine.wanted]["name"]


def cmd_transcribe(a):
    _engine.model_id = a.model
    segments = transcribe_path(a.path, a.diarize)
    print(store.render_file(segments, Path(a.path).resolve(), model_name(), a.format, a.timestamps))
    if a.save:
        log(f"Saved {store.save_file_transcript(a.path, segments, model_name(), a.timestamps, a.format)}")


def cmd_list(a):
    kind = store.kind_from(a.kind) if a.kind else None
    if a.kind and not kind:
        sys.exit(f"Unknown kind '{a.kind}'. Use dictations | calls | files.")
    rows = store.entries(kind)
    if a.json:
        print(json.dumps([{"id": i, "kind": k, "modified": datetime.fromtimestamp(m).isoformat(timespec="seconds"),
                           "path": str(p), "preview": store.preview(p)} for k, i, p, m in rows], ensure_ascii=False, indent=2))
    else:
        print(store.list_text(rows))


def cmd_show(a):
    kind = "calls" if a.call else "dictations" if a.dictation else "files" if a.file else None
    entry = store.resolve(a.id, kind)
    if not entry:
        sys.exit(f"No transcript matching '{a.id}'.")
    print(entry[2].read_text(encoding="utf-8"))


def cmd_status(a):
    info = {
        "version": version("voicely"),
        "model": model_name(),
        "models_ready": engine.models_ready(),
        "models_dir": str(engine.MODELS),
        "transcripts_dir": str(store.BASE),
        "ffmpeg": shutil.which("ffmpeg"),
        "counts": {k: len(store.entries(k)) for k in store.KINDS},
    }
    print(json.dumps(info, indent=2) if a.json else "\n".join(f"{k}: {v}" for k, v in info.items()))


def cmd_download(a):
    engine.ensure_models(a.model)
    log("Models ready.")


def cmd_models(a):
    current = engine.selected_model()
    for model_id, m in engine.CATALOG.items():
        mark, state = "*" if model_id == current else " ", "downloaded" if engine.model_ready(model_id) else ""
        print(f"{mark} {model_id:22} {m['name']:17} {m['size']:8} {state:11} {m['hint']}")


def cmd_connect(a):
    """Register `voicely mcp` with each installed agent through its own CLI."""
    exe = shutil.which("voicely") or sys.argv[0]
    harnesses = {
        "claude": ["claude", "mcp", "add", "--scope", "user", "voicely", "--", exe, "mcp"],
        "codex": ["codex", "mcp", "add", "voicely", "--", exe, "mcp"],
        "cursor": ["cursor", "--add-mcp", json.dumps({"name": "voicely", "command": exe, "args": ["mcp"]})],
    }
    names = [a.harness] if a.harness else list(harnesses)
    found = False
    for name in names:
        if name not in harnesses:
            sys.exit(f"Unknown harness '{name}'. Use: {', '.join(harnesses)}")
        cmd = harnesses[name]
        tool = shutil.which(cmd[0])
        if not tool:
            log(f"{name}: not installed, skipped")
            continue
        found = True
        r = subprocess.run([tool, *cmd[1:]], capture_output=True, text=True)
        out = (r.stdout + r.stderr).strip()
        log(f"{name}: {'registered' if r.returncode == 0 else 'failed'}{' — ' + out if out else ''}")
    if not found:
        log('Nothing to register. Manual config: {"mcpServers":{"voicely":{"command":"voicely","args":["mcp"]}}}')


def cmd_mcp(a):
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError  # only ToolError text reaches the client

    server = MCPServer("voicely", version=version("voicely"))

    @server.tool()
    def transcribe_file(path: str, diarize: bool = False, language: Literal["auto", "ru", "en"] = "auto") -> str:
        """Transcribe an audio or video file offline with Voicely (the selected local model, language auto-detected).
        Optionally run speaker diarization. Returns the transcript text (speaker-labelled when diarize=true)
        and saves it under Documents\\Voicely\\files."""
        try:
            segments = transcribe_path(path, diarize)
        except (FileNotFoundError, RuntimeError) as e:
            raise ToolError(str(e)) from e
        saved = store.save_file_transcript(path, segments, model_name())
        return store.body(segments, markdown=False) + f"\n\n(saved: {saved})"

    @server.tool()
    def list_transcripts(kind: Kind | None = None, limit: int = 100) -> str:
        """List saved Voicely transcripts (dictations, calls, files): modified date, kind, id and a short preview, newest first."""
        return store.list_text(store.entries(kind), max(0, limit))

    @server.tool()
    def get_transcript(id: str, kind: Kind | None = None) -> str:
        """Read a saved transcript by id, or by alias: last | last-call | last-file | last-dictation. Returns the full text."""
        entry = store.resolve(id, kind)
        if not entry:
            raise ToolError(f"No transcript matching '{id}'.")
        return entry[2].read_text(encoding="utf-8") or "(empty transcript)"

    @server.tool()
    def get_last_call() -> str:
        """Read the most recent call transcript ('show me the last call'). Returns the full text."""
        entry = store.resolve("last-call")
        if not entry:
            raise ToolError(f"No call transcripts under {store.BASE / 'calls'}.")
        return entry[2].read_text(encoding="utf-8")

    server.run()


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="voicely", description="Offline transcription + diarization for any agent.")
    p.add_argument("--version", action="version", version=version("voicely"))
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("transcribe", help="Transcribe an audio or video file")
    t.add_argument("path")
    t.add_argument("--diarize", action="store_true", help="label who spoke when")
    t.add_argument("--format", choices=["md", "txt", "jsonl"], default="md")
    t.add_argument("--language", choices=["auto", "ru", "en"], default="auto",
                   help="accepted for compatibility; the models detect the language themselves")
    t.add_argument("--model", choices=list(engine.CATALOG), help="default: the one selected in the tray menu")
    t.add_argument("--timestamps", action="store_true", help="per-segment timestamps in md/txt")
    t.add_argument("--save", action="store_true", help="also save under Documents\\Voicely\\files")
    t.set_defaults(fn=cmd_transcribe)

    li = sub.add_parser("list", help="List saved transcripts, newest first")
    li.add_argument("kind", nargs="?", help="dictations | calls | files")
    li.add_argument("--json", action="store_true")
    li.set_defaults(fn=cmd_list)

    s = sub.add_parser("show", help="Print a transcript by id or alias (last, last-call, last-file, last-dictation)")
    s.add_argument("id")
    for flag in ("call", "dictation", "file"):
        s.add_argument(f"--{flag}", action="store_true", help=f"restrict to {flag}s")
    s.set_defaults(fn=cmd_show)

    st = sub.add_parser("status", help="Model, folders and transcript counts")
    st.add_argument("--json", action="store_true")
    st.set_defaults(fn=cmd_status)

    d = sub.add_parser("download", help="Download a speech model (default: the selected one) and the diarization models")
    d.add_argument("model", nargs="?", choices=list(engine.CATALOG))
    d.set_defaults(fn=cmd_download)
    sub.add_parser("models", help="List speech models; * marks the selected one").set_defaults(fn=cmd_models)
    sub.add_parser("mcp", help="Run the stdio MCP server").set_defaults(fn=cmd_mcp)
    c = sub.add_parser("connect", help="Register the MCP server with installed agents (claude, codex, cursor)")
    c.add_argument("harness", nargs="?")
    c.set_defaults(fn=cmd_connect)

    a = p.parse_args()
    try:
        a.fn(a)
    except (FileNotFoundError, RuntimeError) as e:
        sys.exit(str(e))
