# Voicely for Windows

Windows port of [Voicely](https://github.com/StulkovLD/Voicely) (macOS, Swift; via the [hackhappy/Voicely](https://github.com/hackhappy/Voicely) fork and its Voicely Lab model picker). Same features, same file layout — rebuilt on Python + sherpa-onnx.

- **Dictation** — `Alt+Space`, speak, `Alt+Space` again: text lands at the caret (clipboard + Ctrl+V with your clipboard restored and kept out of Win+V history; typed into terminals). No target → it stays on the clipboard, the pill says "press Ctrl+V".
- **Calls** — tray → Record Call: microphone + system audio (WASAPI loopback), mixed, transcribed, diarized; your voice is `You`.
- **Files** — tray → Transcribe File..., or `voicely transcribe <file> [--diarize]`.
- **MCP** — `voicely mcp` (tools: `transcribe_file`, `list_transcripts`, `get_transcript`, `get_last_call`); `voicely connect` registers it with Claude Code / Codex / Cursor.
- **Offline** — every model below runs on-device; speakers are separated with pyannote segmentation 3.0 + WeSpeaker.

## Install / update

```powershell
.\install.ps1
```

Needs `uv` and `ffmpeg` on PATH. Puts Python, the app and the default model (~670 MB with diarization) in `~\.voicely`, `voicely.exe` in `~\.local\bin`, and a **Voicely** shortcut in the Start Menu.

## Models

Tray → **Model**: a model marked `[downloaded]` is a switch; any other asks first, then downloads. **Delete Downloaded Model** frees the disk space. The choice also applies to the CLI and the MCP server.

| Id | Model | Download | Languages | RAM | Speed* |
|---|---|---|---|---|---|
| `parakeet-ultra` (default) | Parakeet Ultra | ~640 MB | 25 European | ~1.6 GB | 2.7× |
| `parakeet-tdt-0.6b-v3` | Parakeet V3 | ~465 MB | 25 European | ~1.5 GB | 2.8× |
| `gigaam-v3-ru` | GigaAM V3 RU | ~160 MB | Russian only | ~1.2 GB | 5.5× |
| `whisper-turbo` | Whisper Turbo | ~540 MB | any | ~2.5 GB | 1.1× |
| `whisper-large-v3` | Whisper Large V3 | ~1 GB | any | ~4.3 GB | 0.3× |

\* Times faster than real time on an i7-13700HX CPU with another process holding 12 cores, so a loaded machine; expect about three times more when it is idle. RAM is the peak on a 38-second recording.

A model loads when you press the hotkey and leaves memory after 5 minutes idle, or after 1 minute behind a fullscreen game or when RAM is over 85% full. `Logs\voicely.log` records each unload.

Parakeet Ultra is a community int8 conversion of `moondream/parakeet-ultra`, so its download is pinned to one revision and every file is checked against a SHA-256 before it is used. Whisper gives no word timing, so its `transcript.jsonl` and `.srt` are per speech segment.

## Where things are

| What | Where |
|---|---|
| Transcripts | `Documents\Voicely\{dictations,calls,files}` |
| Models, settings, logs | `~\.voicely\` (`config.json`, `Logs\voicely.log`, `Logs\insertion.jsonl`) |
| Failed dictation audio | `~\.voicely\failed-dictations\` |

Autostart: copy the Start Menu shortcut into `shell:startup`.

## CLI

```
voicely transcribe <file> [--model <id>] [--diarize] [--format md|txt|jsonl] [--timestamps] [--save]
voicely models
voicely download [<id>]
voicely list [dictations|calls|files] [--json]
voicely show <id|last|last-call|last-file|last-dictation>
voicely status | mcp | connect [claude|codex|cursor]
```

## Credits

Parakeet TDT 0.6B v3 — NVIDIA (CC-BY-4.0). Parakeet Ultra — moondream (CC-BY-4.0), sherpa-onnx conversion by Alexqwqw. GigaAM — SberDevices (MIT). Whisper — OpenAI (MIT). Runtime — [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) (Apache-2.0).
