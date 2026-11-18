# stems

One command: YouTube link in, separated stems out.

```
stems "https://www.youtube.com/watch?v=XXXX"
```

It downloads the audio, saves it as a 44.1 kHz WAV, and separates it in two
stages with [python-audio-separator](https://github.com/nomadkaraoke/python-audio-separator):

1. **BS-Roformer** (`model_bs_roformer_ep_317_sdr_12.9755`) splits **vocals**
   from the **instrumental**. Roformer models are the current best at vocals.
2. **Demucs `htdemucs_6s`** splits that instrumental into **drums, bass,
   guitar, piano, other**. Running it on a vocal-free track keeps vocal
   bleed out of the instrument stems.

Results land in `~/Music/Stems/<video_title>/` (lowercase, spaces become underscores) and Finder opens the folder:

```
~/Music/Stems/song_title/
  song_title.wav     (full mix)
  vocals.wav  instrumental.wav
  drums.wav  bass.wav  guitar.wav  piano.wav  other.wav
```

Only use it on audio you have the rights to use.

## Install (once)

```
cd ~/repo/stems && ./install.sh
```

Needs Homebrew. It installs ffmpeg and uv if missing, builds a Python 3.12
environment, and puts a `stems` command in `~/.local/bin`. The first run
also downloads the models (~700 MB) to `~/.cache/stems/models`. On Apple
Silicon it uses the GPU (MPS) automatically.

## Options

| Option | What it does |
|---|---|
| `stems song.mp3` | Use a local file instead of a URL |
| `--two-stems` | Only vocals + instrumental (skips stage 2, much faster) |
| `--mp3` | Write stems as 320k MP3 instead of WAV |
| `-o ~/Desktop/Stems` | Different output folder (or set `STEMS_OUT`) |
| `--name "My Take"` | Override the folder name |
| `--vocal-model NAME` | Different stage 1 model, e.g. `vocals_mel_band_roformer.ckpt` |
| `--inst-model NAME` | Different stage 2 model, e.g. `htdemucs_ft.yaml` (no guitar/piano) |

List every available model with `~/repo/stems/.venv/bin/audio-separator --list_models`.

## Background services (local web page + job queue)

This tool is meant to run locally on your own Mac. `daemon/` adds two login
agents. One is a web page at http://localhost:8765 where you can play stems,
download a song as a .zip, or paste a link to queue a job. The other is a
worker that runs those jobs. The page is reachable only from this Mac. See
[daemon/README.md](daemon/README.md).

Flags for scripts: `--no-open` (don't open Finder), `--result-json PATH`
(writes `{dir, files, link}`), `--cpu`.

## Troubleshooting

- **YouTube download errors**: YouTube changes often; update yt-dlp with
  `cd ~/repo/stems && uv lock --upgrade-package yt-dlp && uv sync`.
