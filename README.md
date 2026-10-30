# Voicely for Windows

Windows port of [Voicely](https://github.com/StulkovLD/Voicely) (macOS, Swift; via the [hackhappy/Voicely](https://github.com/hackhappy/Voicely) fork). Same features, same speech model, same file layout — rebuilt on Python + sherpa-onnx.

- **Dictation** — `Alt+Space`, speak, `Alt+Space` again: text lands at the caret (clipboard + Ctrl+V with your clipboard restored and kept out of Win+V history; typed into terminals). No target → it stays on the clipboard, the pill says "press Ctrl+V".
- **Calls** — tray → Record Call: microphone + system audio (WASAPI loopback), mixed, transcribed, diarized; your voice is `You`.
- **Files** — tray → Transcribe File..., or `voicely transcribe <file> [--diarize]`.
- **MCP** — `voicely mcp` (tools: `transcribe_file`, `list_transcripts`, `get_transcript`, `get_last_call`); `voicely connect` registers it with Claude Code / Codex / Cursor.
- **Offline** — NVIDIA Parakeet TDT 0.6B v3 (25 languages incl. Russian and English), pyannote segmentation 3.0 + WeSpeaker.

## Install / update

```powershell
.\install.ps1
```

Needs `uv` and `ffmpeg` on PATH. Puts Python, the app and models (~520 MB) in `~\.voicely`, `voicely.exe` in `~\.local\bin`, and a **Voicely** shortcut in the Start Menu.

## Where things are

| What | Where |
|---|---|
| Transcripts | `Documents\Voicely\{dictations,calls,files}` |
| Models, settings, logs | `~\.voicely\` (`config.json`, `Logs\voicely.log`, `Logs\insertion.jsonl`) |
| Failed dictation audio | `~\.voicely\failed-dictations\` |

Autostart: copy the Start Menu shortcut into `shell:startup`.

## CLI

```
voicely transcribe <file> [--diarize] [--format md|txt|jsonl] [--timestamps] [--save]
voicely list [dictations|calls|files] [--json]
voicely show <id|last|last-call|last-file|last-dictation>
voicely status | download | mcp | connect [claude|codex|cursor]
```
