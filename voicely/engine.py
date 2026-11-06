"""On-device speech engine: a choice of speech models + pyannote/WeSpeaker diarization via sherpa-onnx."""
import ctypes
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
import win32api

SR = 16000
# Not %LOCALAPPDATA%: MSIX-packaged apps (Claude Desktop launching `voicely mcp`) get a private copy of it.
HOME = Path(os.environ.get("VOICELY_HOME") or Path.home() / ".voicely")
MODELS = HOME / "models"
CONFIG = HOME / "config.json"
SEGMENTATION = MODELS / "sherpa-onnx-pyannote-segmentation-3-0"
EMBEDDING = MODELS / "wespeaker_en_voxceleb_resnet34_LM.onnx"
VAD = MODELS / "silero_vad.onnx"

_REL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
SUPPORT = {  # shared by every speech model; target -> url, .tar.bz2 archives hold a folder named like the target
    VAD: _REL + "asr-models/silero_vad.onnx",
    EMBEDDING: _REL + "speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx",
    SEGMENTATION: _REL + "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
}
# Voicely Lab's line-up as far as sherpa-onnx builds exist; adding a model is a data change here.
CATALOG = {
    "parakeet-ultra": {
        "name": "Parakeet Ultra", "size": "~640 MB", "hint": "Most accurate Parakeet, 25 European languages",
        "dir": "parakeet-ultra-sherpa-onnx-int8",
        # A community conversion of moondream/parakeet-ultra: pinned to a revision and checked file by file.
        "base": "https://huggingface.co/Alexqwqw/parakeet-ultra-sherpa-onnx-int8/resolve/3e585661a664da28ff5d9293c5baaca22f930b8e/",
        "files": {
            "encoder.int8.onnx": "4ef28650a81a38bc05124745dcfe776ff2bca7ac1d6009893a0f87d64bdf8b43",
            "decoder.int8.onnx": "94373c6405c8149d1c9d1e9862514e0e3e88dab1c7de18fac9d5df9c6ba5efdd",
            "joiner.int8.onnx": "8a71aaccfdba3d451775507a889afd2257d2e754463fd81095a8c8f290f924c2",
            "tokens.txt": "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d",
        },
    },
    "parakeet-tdt-0.6b-v3": {
        "name": "Parakeet V3", "size": "~465 MB", "hint": "Fast, 25 European languages",
        "dir": "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
        "url": _REL + "asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2",
    },
    "gigaam-v3-ru": {
        "name": "GigaAM V3 RU", "size": "~160 MB", "hint": "Best and fastest in Russian, Russian only",
        "dir": "sherpa-onnx-nemo-transducer-punct-giga-am-v3-russian-2025-12-16",
        "url": _REL + "asr-models/sherpa-onnx-nemo-transducer-punct-giga-am-v3-russian-2025-12-16.tar.bz2",
        "decoder": "decoder.onnx", "joiner": "joiner.onnx",
    },
    "omnilingual-1b": {
        "name": "Omnilingual 1B", "size": "~750 MB", "hint": "1600 languages incl. Armenian, no punctuation",
        "dir": "sherpa-onnx-omnilingual-asr-1600-languages-1B-ctc-v2-int8-2026-02-05", "omnilingual": True,
        "url": _REL + "asr-models/sherpa-onnx-omnilingual-asr-1600-languages-1B-ctc-v2-int8-2026-02-05.tar.bz2",
    },
    "whisper-turbo": {
        "name": "Whisper Turbo", "size": "~540 MB", "hint": "Any language, slow on CPU, 2.5 GB RAM",
        "dir": "sherpa-onnx-whisper-turbo", "whisper": "turbo",
        "url": _REL + "asr-models/sherpa-onnx-whisper-turbo.tar.bz2",
    },
    "whisper-large-v3": {
        "name": "Whisper Large V3", "size": "~1 GB", "hint": "Any language, very slow on CPU, 4.3 GB RAM",
        "dir": "sherpa-onnx-whisper-large-v3", "whisper": "large-v3",
        "url": _REL + "asr-models/sherpa-onnx-whisper-large-v3.tar.bz2",
    },
}
DEFAULT_MODEL = "parakeet-ultra"
# Whole-clip decoding up to this length; longer audio is cut at pauses by the VAD.
MAX_DIRECT_SECONDS = 25
THREADS = max(1, (os.cpu_count() or 2) // 2)
# A loaded Parakeet holds 0.7-1.6 GB of RAM (Whisper up to 4.3 GB), so it only stays while it is in use.
IDLE_UNLOAD_SECONDS = 300
BUSY_UNLOAD_SECONDS = 60  # grace period: back-to-back dictations in a game stay fast
WATCH_SECONDS = 15
RAM_BUSY_PERCENT = 85


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def read_config():
    try:
        return json.loads(CONFIG.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def selected_model():
    model = read_config().get("model")
    return model if model in CATALOG else DEFAULT_MODEL


def model_dir(model_id):
    return MODELS / CATALOG[model_id]["dir"]


def model_ready(model_id):
    return model_dir(model_id).exists()  # the folder takes its final name only when complete


def models_ready(model_id=None):
    return model_ready(model_id or selected_model()) and all(p.exists() for p in SUPPORT)


def _fetch(url, path, progress):
    name = url.rsplit("/", 1)[1]
    seen = [-1]

    def hook(blocks, block_size, total):
        pct = min(100, blocks * block_size * 100 // total) if total > 0 else 0
        if pct // 10 != seen[0]:
            seen[0] = pct // 10
            progress(f"Downloading {name}: {pct}%")

    urllib.request.urlretrieve(url, path, hook)


def _install(target, url, progress):
    """One file or .tar.bz2 archive; it takes its final name only when complete."""
    part = MODELS / (url.rsplit("/", 1)[1] + ".part")
    _fetch(url, part, progress)
    if url.endswith(".tar.bz2"):
        staging = MODELS / f".{target.name}.tmp"
        shutil.rmtree(staging, ignore_errors=True)
        with tarfile.open(part) as tar:
            tar.extractall(staging, filter="data")
        (staging / target.name).replace(target)
        shutil.rmtree(staging)
        part.unlink()
    else:
        part.replace(target)


def _install_files(target, base, files, progress):
    """Pinned single files into a folder. A wrong hash stops it, so nothing unverified is ever loaded."""
    staging = MODELS / f".{target.name}.tmp"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    for name, sha256 in files.items():
        _fetch(base + name, staging / name, progress)
        with open(staging / name, "rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
        if digest != sha256:
            shutil.rmtree(staging, ignore_errors=True)
            raise RuntimeError(f"{name} failed the integrity check")
    staging.replace(target)


def ensure_models(model_id=None, progress=log):
    """Download whatever is missing for this speech model."""
    MODELS.mkdir(parents=True, exist_ok=True)
    for target, url in SUPPORT.items():
        if not target.exists():
            _install(target, url, progress)
    model_id = model_id or selected_model()
    if not model_ready(model_id):
        model = CATALOG[model_id]
        if "files" in model:
            _install_files(model_dir(model_id), model["base"], model["files"], progress)
        else:
            _install(model_dir(model_id), model["url"], progress)


def system_busy():
    """A fullscreen game or app is in front, or RAM is nearly full."""
    state = ctypes.c_int()
    ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
    # QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE
    return state.value in (2, 3, 4) or win32api.GlobalMemoryStatusEx()["MemoryLoad"] >= RAM_BUSY_PERCENT


def load_audio(path):
    """Any audio/video file -> 16 kHz mono float32 (needs ffmpeg on PATH)."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        raise RuntimeError("ffmpeg not found on PATH (install it: winget install Gyan.FFmpeg)") from None
    if out.returncode:
        raise RuntimeError(out.stderr.decode(errors="replace").strip() or "ffmpeg could not decode the file")
    return np.frombuffer(out.stdout, np.float32).copy()


class Engine:
    """Lazily loaded recognizer + diarizer, shared by the tray app, CLI and MCP server."""

    def __init__(self):
        self._lock = threading.Lock()
        self._asr = self._loaded = self._vad_config = self._diarizer = self._watcher = None
        self.model_id = None  # None follows the selection in config.json; `--model` pins it
        self.last_used = time.monotonic()

    @property
    def wanted(self):
        return self.model_id or selected_model()

    def delete(self, model_id):
        with self._lock:
            if self._loaded == model_id:
                self._asr = None
            shutil.rmtree(model_dir(model_id))

    def load(self):
        with self._lock:
            self._recognizer()
            self.last_used = time.monotonic()

    def _should_unload(self):
        idle = time.monotonic() - self.last_used
        return idle > IDLE_UNLOAD_SECONDS or (idle > BUSY_UNLOAD_SECONDS and system_busy())

    def _watch(self):
        while True:
            time.sleep(WATCH_SECONDS)
            with self._lock:  # waits out a running transcription, which then counts as fresh use
                if (self._asr or self._diarizer) and self._should_unload():
                    self._asr = self._diarizer = None
                    logging.getLogger("voicely").info("model unloaded")

    def _recognizer(self):
        if self._watcher is None:
            self._watcher = threading.Thread(target=self._watch, daemon=True)
            self._watcher.start()
        wanted = self.wanted
        if self._asr is None or self._loaded != wanted:
            self._asr = None  # free the old model first: never two in RAM
            ensure_models(wanted)
            import sherpa_onnx

            path, prefix = model_dir(wanted), CATALOG[wanted].get("whisper")
            if prefix:
                self._asr = sherpa_onnx.OfflineRecognizer.from_whisper(
                    encoder=str(path / f"{prefix}-encoder.int8.onnx"),
                    decoder=str(path / f"{prefix}-decoder.int8.onnx"),
                    tokens=str(path / f"{prefix}-tokens.txt"),
                    language="",  # detect it
                    num_threads=THREADS,
                )
            elif CATALOG[wanted].get("omnilingual"):
                self._asr = sherpa_onnx.OfflineRecognizer.from_omnilingual_asr_ctc(
                    model=str(path / "model.int8.onnx"), tokens=str(path / "tokens.txt"), num_threads=THREADS,
                )
            else:
                self._asr = sherpa_onnx.OfflineRecognizer.from_transducer(
                    encoder=str(path / "encoder.int8.onnx"),
                    decoder=str(path / CATALOG[wanted].get("decoder", "decoder.int8.onnx")),
                    joiner=str(path / CATALOG[wanted].get("joiner", "joiner.int8.onnx")),
                    tokens=str(path / "tokens.txt"),
                    model_type="nemo_transducer",
                    num_threads=THREADS,
                )
            self._loaded = wanted
            self._vad_config = sherpa_onnx.VadModelConfig(
                silero_vad=sherpa_onnx.SileroVadModelConfig(
                    model=str(VAD), min_silence_duration=0.4, min_speech_duration=0.25, max_speech_duration=20,
                ),
                sample_rate=SR,
            )
        return self._asr

    def _chunks(self, samples):
        """(offset_seconds, samples) pieces: the whole clip if short, else VAD speech regions."""
        # Whisper invents sentences on silence, so it only ever hears what the VAD calls speech.
        if len(samples) <= MAX_DIRECT_SECONDS * SR and "whisper" not in CATALOG[self._loaded]:
            yield 0.0, samples
            return
        import sherpa_onnx

        vad = sherpa_onnx.VoiceActivityDetector(self._vad_config, buffer_size_in_seconds=60)
        window = 512
        for i in range(0, len(samples), window):
            vad.accept_waveform(samples[i:i + window])
            while not vad.empty():
                yield vad.front.start / SR, np.asarray(vad.front.samples, np.float32)
                vad.pop()
        vad.flush()
        while not vad.empty():
            yield vad.front.start / SR, np.asarray(vad.front.samples, np.float32)
            vad.pop()

    def transcribe(self, samples):
        """16 kHz float32 -> sentence segments [{start, end, text, words: [{w, s, e}]}] in seconds."""
        samples = np.asarray(samples, np.float32)
        if len(samples) < SR // 10:
            return []
        with self._lock:
            asr = self._recognizer()
            segments = []
            for offset, chunk in self._chunks(samples):
                stream = asr.create_stream()
                # Trailing silence lets the transducer flush the last word.
                stream.accept_waveform(SR, np.concatenate([chunk, np.zeros(SR // 2, np.float32)]))
                asr.decode_stream(stream)
                r = stream.result
                end = offset + len(chunk) / SR
                if r.timestamps:
                    segments += sentences(r.tokens, r.timestamps, offset, end)
                elif r.text.strip():  # Whisper gives no token timing: the chunk is the segment
                    segments.append({"start": offset, "end": end, "text": r.text.strip(), "words": []})
            self.last_used = time.monotonic()
            return segments

    def diarize(self, samples):
        """16 kHz float32 -> [(start, end, speaker_index)] sorted by start."""
        with self._lock:
            if self._diarizer is None:
                ensure_models(self.wanted)
                import sherpa_onnx

                config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
                    segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                        pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                            model=str(SEGMENTATION / "model.onnx")
                        ),
                        num_threads=THREADS,
                    ),
                    embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMBEDDING), num_threads=THREADS),
                    clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=0.5),
                    min_duration_on=0.3,
                    min_duration_off=0.5,
                )
                if not config.validate():
                    raise RuntimeError("diarization models are incomplete")
                self._diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)
            result = self._diarizer.process(np.asarray(samples, np.float32)).sort_by_start_time()
            self.last_used = time.monotonic()
            return [(s.start, s.end, s.speaker) for s in result]


def sentences(tokens, times, offset, chunk_end):
    """BPE tokens with start times -> sentence segments carrying word-level timing."""
    words = []
    for tok, t in zip(tokens, times):
        if not words or tok.startswith((" ", "▁")):
            words.append({"w": tok.replace("▁", " ").strip(), "s": round(offset + t, 2)})
        else:
            words[-1]["w"] += tok
    words = [w for w in words if w["w"]]
    for i, w in enumerate(words):
        nxt = words[i + 1]["s"] if i + 1 < len(words) else chunk_end
        w["e"] = round(min(nxt, w["s"] + 1.5), 2)  # a pause after a word is not part of it
    out, cur = [], []
    for w in words:
        cur.append(w)
        if w["w"].endswith((".", "?", "!", "…")):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [{"start": ws[0]["s"], "end": ws[-1]["e"], "text": " ".join(x["w"] for x in ws), "words": ws} for ws in out]


def label_speakers(segments, turns, you=None):
    """Give each segment the diarized speaker it overlaps most: 'You' for index `you`, else 'Speaker N'."""
    names = {}
    for seg in segments:
        best, overlap = None, 0.0
        for start, end, spk in turns:
            o = min(seg["end"], end) - max(seg["start"], start)
            if o > overlap:
                best, overlap = spk, o
        if best is None and turns:  # no overlap: nearest turn
            best = min(turns, key=lambda t: min(abs(t[0] - seg["end"]), abs(t[1] - seg["start"])))[2]
        if best is None:
            seg["speaker"] = "Speaker 1"
            continue
        if best == you:
            seg["speaker"] = "You"
        else:
            names.setdefault(best, f"Speaker {len(names) + 1}")
            seg["speaker"] = names[best]
        seg["speaker_id"] = int(best)
    return segments
