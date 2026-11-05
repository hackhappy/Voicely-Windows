"""Transcripts as plain files under Documents\\Voicely — same layout as the macOS Voicely:

    dictations\\<stamp>-<rand>.md
    calls\\<stamp>\\transcript.md (+ transcript.jsonl, mix.wav, mic.wav, system.wav)
    files\\<name>\\transcript.md (+ transcript.srt)
"""
import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path

KINDS = ("dictations", "calls", "files")
BLOCK_GAP = 6.0  # silence inside one speaker's turn that still reads as the same thought


def _documents():
    try:
        from win32com.shell import shell, shellcon

        return Path(shell.SHGetKnownFolderPath(shellcon.FOLDERID_Documents))  # follows OneDrive redirection
    except Exception:
        return Path.home() / "Documents"


BASE = Path(os.environ.get("VOICELY_TRANSCRIPTS") or _documents() / "Voicely")


def stamp(dt=None):
    return (dt or datetime.now()).strftime("%Y-%m-%d_%H-%M-%S-%f")[:-3]


def iso(dt=None):
    return (dt or datetime.now()).astimezone().isoformat(timespec="seconds")


def clock(seconds):
    s = int(round(max(0, seconds)))
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def _yaml(value):
    value = str(value)
    if "\n" in value:
        return json.dumps(value, ensure_ascii=False)
    # single quotes keep Windows paths readable: no backslash escaping in YAML '...'
    return "'" + value.replace("'", "''") + "'" if re.search(r'[:#\'"]|^\s|\s$', value) else value


def _write(path, text):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ---------- writing ----------

def save_dictation(text, source_app):
    folder = BASE / "dictations"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stamp()}-{secrets.token_hex(3)}.md"
    _write(path, f"---\ntype: dictation\ndate: {iso()}\nsource_app: {_yaml(source_app or 'unknown')}\n---\n\n{text}\n")
    return path


def turns(segments):
    """Merge consecutive segments of one speaker into blocks: [(start, speaker, text)]."""
    blocks, end = [], -1e9
    for s in segments:
        if blocks and blocks[-1][1] == s.get("speaker") and s["start"] - end <= BLOCK_GAP:
            blocks[-1][2].append(s["text"])
        else:
            blocks.append((s["start"], s.get("speaker"), [s["text"]]))
        end = s["end"]
    return [(start, spk, " ".join(texts)) for start, spk, texts in blocks]


def speakers_summary(segments):
    names = {s["speaker"] for s in segments if s.get("speaker")}
    others = len(names - {"You"})
    if "You" in names:
        return f"You + {others}" if others else "You"
    return str(others) if others else "none"


def jsonl(segments, language=""):
    return "\n".join(
        json.dumps(
            {"t": round(s["start"], 2), "d": round(s["end"] - s["start"], 2), "lang": language,
             "text": s["text"], **({"speaker": s["speaker"]} if s.get("speaker") else {}),
             **({"speaker_id": s["speaker_id"]} if "speaker_id" in s else {}), "words": s.get("words", [])},
            ensure_ascii=False, sort_keys=True,
        )
        for s in segments
    )


def body(segments, timestamps=False, markdown=True):
    """Transcript text: speaker blocks when diarized, else paragraphs (or timestamped lines)."""
    if not segments:
        return "(No speech detected)"
    if any(s.get("speaker") for s in segments):
        if markdown:
            return "\n\n".join(f"[{clock(t)}] {spk}\n{text}" for t, spk, text in turns(segments))
        return "\n".join(f"[{clock(t)}] {spk}: {text}" if timestamps else f"{spk}: {text}" for t, spk, text in turns(segments))
    if timestamps:
        return "\n".join(f"[{clock(s['start'])}] {s['text']}" for s in segments)
    paras, cur, end = [], [], None
    for s in segments:
        if cur and (len(cur) >= 5 or s["start"] - end > 2.0):
            paras.append(" ".join(cur))
            cur = []
        cur.append(s["text"])
        end = s["end"]
    paras.append(" ".join(cur))
    return "\n\n".join(paras)


def render_file(segments, source, model, fmt="md", timestamps=False):
    if fmt == "jsonl":
        return jsonl(segments)
    text = body(segments, timestamps, markdown=fmt == "md")
    if fmt == "txt":
        return text
    return f"---\ntype: file-transcription\nsource: {_yaml(source)}\ndate: {iso()}\nlanguage: auto\nmodel: {model}\n---\n\n{text}\n"


def srt(segments):
    def ts(sec):
        ms = int(round(max(0, sec) * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    return "\n".join(
        f"{i}\n{ts(s['start'])} --> {ts(s['end'])}\n{(s['speaker'] + ': ') if s.get('speaker') else ''}{s['text']}\n"
        for i, s in enumerate(segments, 1)
    )


def save_file_transcript(source, segments, model, timestamps=False, fmt="md"):
    root = BASE / "files"
    root.mkdir(parents=True, exist_ok=True)
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(source).stem).strip(" .") or "file"
    folder, i = root / base, 1
    while folder.exists():
        i += 1
        folder = root / f"{base}-{i}"
    folder.mkdir()
    ext = "txt" if fmt == "txt" else "md"
    path = folder / f"transcript.{ext}"
    _write(path, render_file(segments, Path(source).resolve(), model, "txt" if fmt == "txt" else "md", timestamps))
    _write(folder / "transcript.srt", srt(segments))
    return path


def save_call(folder, segments, started, source_app=None):
    duration = clock(max((s["end"] for s in segments), default=0))
    md = (
        f"---\ntype: call\ndate: {iso(started)}\nsource_app: {_yaml(source_app or 'unknown')}\n"
        f"duration: {duration}\nspeakers: {speakers_summary(segments)}\n---\n\n{body(segments)}\n"
    )
    _write(folder / "transcript.jsonl", jsonl(segments) + "\n")
    _write(folder / "transcript.md", md)
    return folder / "transcript.md"


# ---------- reading ----------

def entries(kind=None):
    """[(kind, id, path, mtime)] newest first."""
    out = []
    for k in [kind] if kind else KINDS:
        folder = BASE / k
        if not folder.is_dir():
            continue
        for p in folder.iterdir():
            if k == "dictations":
                if p.suffix.lower() == ".md":
                    out.append((k, p.stem, p, p.stat().st_mtime))
            elif p.is_dir():
                doc = next((p / n for n in ("transcript.md", "transcript.txt") if (p / n).exists()), None)
                if doc:
                    out.append((k, p.name, doc, doc.stat().st_mtime))
    return sorted(out, key=lambda e: e[3], reverse=True)


def kind_from(token):
    t = (token or "").lower()
    return next((k for k in KINDS if t in (k, k[:-1])), None)


def resolve(id_or_alias, kind=None):
    low = id_or_alias.lower()
    if low.startswith("last-") and kind_from(low[5:]):
        return next(iter(entries(kind_from(low[5:]))), None)
    if low in ("last", "latest"):
        return next(iter(entries(kind)), None)
    return next((e for e in entries(kind) if e[1] == id_or_alias), None)


def preview(path, n=80):
    text = path.read_text(encoding="utf-8", errors="replace")
    if text.startswith("---"):
        text = text.split("\n---", 1)[-1]
    text = " ".join(text.split())
    return text[:n] + ("…" if len(text) > n else "")


def list_text(rows, limit=None):
    rows = rows[:limit] if limit is not None else rows
    if not rows:
        return f"No transcripts under {BASE}"
    return "\n".join(
        f"{datetime.fromtimestamp(m):%Y-%m-%d %H:%M}  {k[:-1]:<9}  {i}  {preview(p)}" for k, i, p, m in rows
    )
