"""On-device speech engine: Parakeet TDT 0.6B v3 + pyannote/WeSpeaker diarization via sherpa-onnx."""
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import urllib.request
from pathlib import Path

import numpy as np

SR = 16000
# Not %LOCALAPPDATA%: MSIX-packaged apps (Claude Desktop launching `voicely mcp`) get a private copy of it.
HOME = Path(os.environ.get("VOICELY_HOME") or Path.home() / ".voicely")
MODELS = HOME / "models"
PARAKEET = MODELS / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
SEGMENTATION = MODELS / "sherpa-onnx-pyannote-segmentation-3-0"
EMBEDDING = MODELS / "wespeaker_en_voxceleb_resnet34_LM.onnx"
VAD = MODELS / "silero_vad.onnx"
MODEL_NAME = "Parakeet V3"

_REL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
DOWNLOADS = {  # target -> url; .tar.bz2 archives hold a folder named like the target
    VAD: _REL + "asr-models/silero_vad.onnx",
    EMBEDDING: _REL + "speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx",
    SEGMENTATION: _REL + "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
    PARAKEET: _REL + "asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2",
}
# Whole-clip decoding up to this length; longer audio is cut at pauses by the VAD.
MAX_DIRECT_SECONDS = 25
THREADS = max(1, (os.cpu_count() or 2) // 2)


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def models_ready():
    return all(p.exists() for p in DOWNLOADS)


def ensure_models(progress=log):
    """Download missing models (~520 MB total). Each lands under its final name only when complete."""
    MODELS.mkdir(parents=True, exist_ok=True)
    for target, url in DOWNLOADS.items():
        if target.exists():
            continue
        name = url.rsplit("/", 1)[1]
        part = MODELS / (name + ".part")
        seen = [-1]

        def hook(blocks, block_size, total, name=name):
            pct = min(100, blocks * block_size * 100 // total) if total > 0 else 0
            if pct // 10 != seen[0]:
                seen[0] = pct // 10
                progress(f"Downloading {name}: {pct}%")

        urllib.request.urlretrieve(url, part, hook)
        if name.endswith(".tar.bz2"):
            staging = MODELS / ".extract"
            shutil.rmtree(staging, ignore_errors=True)
            with tarfile.open(part) as tar:
                tar.extractall(staging, filter="data")
            (staging / target.name).replace(target)
            shutil.rmtree(staging)
            part.unlink()
        else:
            part.replace(target)


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
        self._asr = self._vad_config = self._diarizer = None

    def load(self):
        with self._lock:
            self._recognizer()

    def _recognizer(self):
        if self._asr is None:
            ensure_models()
            import sherpa_onnx

            self._asr = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=str(PARAKEET / "encoder.int8.onnx"),
                decoder=str(PARAKEET / "decoder.int8.onnx"),
                joiner=str(PARAKEET / "joiner.int8.onnx"),
                tokens=str(PARAKEET / "tokens.txt"),
                model_type="nemo_transducer",
                num_threads=THREADS,
            )
            self._vad_config = sherpa_onnx.VadModelConfig(
                silero_vad=sherpa_onnx.SileroVadModelConfig(
                    model=str(VAD), min_silence_duration=0.4, min_speech_duration=0.25, max_speech_duration=20,
                ),
                sample_rate=SR,
            )
        return self._asr

    def _chunks(self, samples):
        """(offset_seconds, samples) pieces: the whole clip if short, else VAD speech regions."""
        if len(samples) <= MAX_DIRECT_SECONDS * SR:
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
                segments += sentences(r.tokens, r.timestamps, offset, offset + len(chunk) / SR)
            return segments

    def diarize(self, samples):
        """16 kHz float32 -> [(start, end, speaker_index)] sorted by start."""
        with self._lock:
            if self._diarizer is None:
                ensure_models()
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
