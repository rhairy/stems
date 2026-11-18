#!/usr/bin/env python3
"""stems: YouTube link (or local audio file) -> WAV -> separated stems.

Two-stage pipeline (python-audio-separator):
  1. A Roformer model splits vocals from the instrumental (best vocal quality).
  2. Demucs htdemucs_6s splits the instrumental into drums, bass, guitar, piano, other.

Usage:
    stems "https://www.youtube.com/watch?v=..."
    stems song.mp3
    stems URL --two-stems          # just vocals + instrumental
    stems URL --mp3 --out ~/Desktop/Stems

Only use this on audio you have the rights to use.
"""
import argparse
import json
import logging
import os
import urllib.parse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_OUT = Path(os.environ.get("STEMS_OUT", Path.home() / "Music" / "Stems")).expanduser()
# Base URL of the local stems web server (e.g. http://localhost:8765). When set, a link to the result is printed.
PUBLIC_URL = os.environ.get("STEMS_PUBLIC_URL", "").rstrip("/")
MODEL_DIR = Path.home() / ".cache" / "stems" / "models"
VOCAL_MODEL = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"  # BS-Roformer (viperx), vocals/instrumental
INST_MODEL = "htdemucs_6s.yaml"  # drums, bass, guitar, piano, other (+ vocals residue, discarded)


def safe_name(name: str) -> str:
    # Lowercase, spaces -> underscores, minus "(Official Video)"-style tags and characters macOS/Windows dislike.
    name = re.sub(r"\s*[\(\[][^\)\]]*\b(official|lyrics?|audio|video|visuali[sz]er|hd|4k|remaster(ed)?)\b[^\)\]]*[\)\]]",
                  "", name, flags=re.I)
    name = re.sub(r'[\\/:*?"<>|]+', " ", name).strip().strip(".")
    return re.sub(r"\s+", "_", name).lower()[:120] or "track"


def is_url(s: str) -> bool:
    return bool(re.match(r"^https?://", s))


def download(url: str, workdir: Path) -> tuple[Path, str]:
    """Download best audio and convert to 44.1 kHz stereo WAV. Returns (wav, title)."""
    import yt_dlp

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
        "postprocessor_args": {"extractaudio": ["-ar", "44100", "-ac", "2"]},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    wav = workdir / f"{info['id']}.wav"
    if not wav.exists():
        sys.exit("Download finished but no WAV was produced. Is ffmpeg installed?")
    return wav, info.get("title") or info["id"]


def to_wav(src: Path, dst: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ar", "44100", "-ac", "2", str(dst)],
        check=True,
    )


def force_cpu() -> None:
    """Hide the GPU from torch so audio-separator picks the CPU."""
    import torch

    torch.backends.mps.is_available = lambda: False
    torch.cuda.is_available = lambda: False


def make_separator(out_dir: Path, mp3: bool):
    from audio_separator.separator import Separator

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    return Separator(
        log_level=logging.WARNING,
        model_file_dir=str(MODEL_DIR),
        output_dir=str(out_dir),
        output_format="MP3" if mp3 else "WAV",
        output_bitrate="320k" if mp3 else None,
    )


def separate(mix: Path, dest: Path, vocal_model: str, inst_model: str, mp3: bool, two_stems: bool) -> None:
    ext = "mp3" if mp3 else "wav"
    with tempfile.TemporaryDirectory(dir=dest) as work:
        work = Path(work)

        # Stage 1: vocals vs. instrumental. Keep the instrumental lossless for stage 2.
        print(f"[1/2] Splitting vocals with {vocal_model} (first run downloads the model)...")
        sep = make_separator(work, mp3=False)
        sep.load_model(model_filename=vocal_model)
        # Vocal-only models call the non-vocal stem "Other"; treat it as the instrumental.
        sep.separate(str(mix), {"Vocals": "vocals", "Instrumental": "instrumental", "Other": "instrumental"})
        vocals, inst = work / "vocals.wav", work / "instrumental.wav"
        if not (vocals.exists() and inst.exists()):
            sys.exit(f"Stage 1 did not produce vocals + instrumental: {[p.name for p in work.iterdir()]}")

        if mp3:
            for src in (vocals, inst):
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-b:a", "320k",
                                str(dest / f"{src.stem}.mp3")], check=True)
        else:
            shutil.copy(vocals, dest / "vocals.wav")
            shutil.copy(inst, dest / "instrumental.wav")

        if two_stems:
            return

        # Stage 2: instruments from the vocal-free instrumental.
        print(f"[2/2] Splitting instruments with {inst_model}...")
        sep = make_separator(dest, mp3=mp3)
        sep.load_model(model_filename=inst_model)
        names = {s: s.lower() for s in ("Drums", "Bass", "Guitar", "Piano", "Other")}
        names["Vocals"] = "vocal_residue"
        sep.separate(str(inst), names)
        (dest / f"vocal_residue.{ext}").unlink(missing_ok=True)


def result_link(dest: Path, out_root: Path) -> str | None:
    if not PUBLIC_URL:
        return None
    rel = dest.relative_to(out_root).as_posix()
    return f"{PUBLIC_URL}/{urllib.parse.quote(rel)}/"


def run(source: str, out: Path = DEFAULT_OUT, name: str | None = None, vocal_model: str = VOCAL_MODEL,
        inst_model: str = INST_MODEL, two_stems: bool = False, mp3: bool = False) -> dict:
    """Download/convert, separate, and return a summary dict (dir, files, link)."""
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found. Install it with: brew install ffmpeg")
    out = out.expanduser()

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if is_url(source):
            print("Downloading audio...")
            wav, title = download(source, tmp)
        else:
            src = Path(source).expanduser()
            if not src.exists():
                sys.exit(f"File not found: {src}")
            title = src.stem
            wav = tmp / "input.wav"
            to_wav(src, wav)

        folder = safe_name(name or title)
        dest = out / folder
        dest.mkdir(parents=True, exist_ok=True)
        original = dest / f"{folder}.wav"
        shutil.copy(wav, original)
        print(f"Saved full mix: {original}")

    separate(original, dest, vocal_model, inst_model, mp3, two_stems)
    return {
        "title": title,
        "name": folder,
        "dir": str(dest),
        "files": sorted(f.name for f in dest.iterdir() if not f.name.startswith(".")),
        "link": result_link(dest, out),
    }


def main() -> None:
    p = argparse.ArgumentParser(prog="stems", description="Download a YouTube video's audio and split it into stems.")
    p.add_argument("source", help="YouTube URL or path to a local audio file")
    p.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT,
                   help=f"Output root (default: $STEMS_OUT or {DEFAULT_OUT})")
    p.add_argument("--vocal-model", default=VOCAL_MODEL, help=f"Stage 1 model (default: {VOCAL_MODEL})")
    p.add_argument("--inst-model", default=INST_MODEL,
                   help=f"Stage 2 model (default: {INST_MODEL}; htdemucs_ft.yaml = no guitar/piano)")
    p.add_argument("--two-stems", action="store_true", help="Only vocals + instrumental (skip stage 2)")
    p.add_argument("--mp3", action="store_true", help="Write stems as 320k MP3 instead of WAV")
    p.add_argument("--name", help="Override the output folder name")
    p.add_argument("--cpu", action="store_true", help="Don't use the GPU (also: STEMS_FORCE_CPU=1)")
    p.add_argument("--no-open", action="store_true", help="Don't open the result folder in Finder")
    p.add_argument("--result-json", type=Path, metavar="PATH",
                   help="Write a JSON summary (dir, files, link) to PATH, for bots and scripts")
    a = p.parse_args()

    if a.cpu or os.environ.get("STEMS_FORCE_CPU") == "1":
        force_cpu()

    res = run(a.source, a.out, a.name, a.vocal_model, a.inst_model, a.two_stems, a.mp3)

    print(f"\nDone. Stems are in: {res['dir']}")
    for f in res["files"]:
        print(f"  {f}")
    if res["link"]:
        print(f"Link: {res['link']}")
    if a.result_json:
        a.result_json.write_text(json.dumps(res, indent=2))
    if sys.platform == "darwin" and not a.no_open and os.environ.get("STEMS_NO_OPEN") != "1":
        subprocess.run(["open", res["dir"]])


if __name__ == "__main__":
    main()
