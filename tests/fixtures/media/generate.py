#!/usr/bin/env python3
"""Generate the committed v1 media sample data.

The generated files are checked in so tests do not need ``ffmpeg``,
``espeak-ng``, or any other generator tool.  Re-run this script only when the
fixture set changes:

    python tests/fixtures/media/generate.py --force

Every file is deliberately tiny.  The set covers the media families the files
provider classifies (text, image, audio, video) and the other servable asset
families (attachments and fonts), plus each text-derived scenario:

* sidecar SubRip and WebVTT captions;
* a malformed/tricky caption file (tags, entities, NOTE, cue indices);
* an embedded ``mov_text`` caption track;
* media with speech audio (ASR), and media with no speech at all;
* an image containing readable text, an image without text, and an
  image-only PDF (OCR);
* a PDF with a native selectable text layer;
* an ambiguous sidecar shared by two media files with the same stem.

``manifest.json`` records the expected classification for every file.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent

SPEECH = "The quick brown fox jumps over the lazy dog."
EMBEDDED_CAPTION = "Embedded caption about a zebra crossing."
SIDECAR_SRT = (
    "1\n"
    "00:00:00,000 --> 00:00:01,000\n"
    "sidecar zebra algebra lesson\n"
    "\n"
    "2\n"
    "00:00:01,000 --> 00:00:02,000\n"
    "second cue about unicorn geometry\n"
)
SIDECAR_VTT = (
    "WEBVTT\n"
    "\n"
    "NOTE this note is ignored by the parser\n"
    "\n"
    "00:00:00.000 --> 00:00:01.000\n"
    "vtt unicorn geometry lesson\n"
    "\n"
    "00:00:01.000 --> 00:00:02.000\n"
    "second <b>vtt</b> cue about algebra\n"
)
TRICKY_SRT = (
    "1\n"
    "00:00:00,000 --> 00:00:01,000\n"
    "<i>tricky</i> hippopotamus &amp; friends\n"
    "\n"
    "NOTE a note that must be skipped\n"
    "\n"
    "2\n"
    "00:00:01,000 --> 00:00:02,000\n"
    "youtube-style tag content\n"
)


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL)


def _write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)


def _ffmpeg(*arguments: str) -> None:
    # ``-map_metadata`` is an output option, so insert it just before the final
    # output path rather than before the inputs.
    argv = list(arguments)
    if argv:
        destination = Path(argv[-1])
        destination.parent.mkdir(parents=True, exist_ok=True)
        argv[-1:-1] = ["-map_metadata", "-1"]
    _run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "+bitexact",
            *argv,
        ]
    )


def _speech_wav(destination: Path, text: str = SPEECH) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(["espeak-ng", "-w", str(destination), "-s", "145", text])


def _render_text_image(destination: Path) -> None:
    """Render a PNG with readable OCR text using PyMuPDF."""

    import pymupdf

    document = pymupdf.open()
    page = document.new_page(width=320, height=120)
    page.insert_text((16, 60), "zebra OCR fixture", fontsize=28)
    page.insert_text((16, 92), "second line 12345", fontsize=16)
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))
    destination.parent.mkdir(parents=True, exist_ok=True)
    pixmap.save(str(destination))
    document.close()


def _render_pdf(destination: Path, text: str) -> None:
    import pymupdf

    document = pymupdf.open()
    page = document.new_page(width=320, height=120)
    page.insert_text((16, 60), text, fontsize=20)
    destination.parent.mkdir(parents=True, exist_ok=True)
    document.save(destination)
    document.close()


def _render_scanned_pdf(destination: Path, image: Path) -> None:
    import pymupdf

    document = pymupdf.open()
    page = document.new_page(width=320, height=120)
    pixmap = pymupdf.Pixmap(str(image))
    compressed = pixmap.tobytes("jpeg", jpg_quality=70)
    page.insert_image(page.rect, stream=compressed)
    destination.parent.mkdir(parents=True, exist_ok=True)
    document.save(destination, deflate=True)
    document.close()


def _build_audio(root: Path, work: Path) -> None:
    speech = work / "speech.wav"
    _speech_wav(speech)
    _ffmpeg("-i", str(speech), "-c:a", "libmp3lame", "-b:a", "32k", "-t", "2", str(root / "audio" / "speech.mp3"))
    _ffmpeg("-i", str(speech), "-c:a", "libopus", "-b:a", "24k", "-t", "2", str(root / "audio" / "speech.ogg"))
    # A tone with no speech: ASR must produce nothing useful.
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        "-ar",
        "8000",
        "-ac",
        "1",
        str(root / "audio" / "tone.wav"),
    )


def _build_video(root: Path, work: Path) -> None:
    speech = work / "speech.wav"
    embedded_srt = work / "embedded.srt"
    _write(embedded_srt, f"1\n00:00:00,000 --> 00:00:01,500\n{EMBEDDED_CAPTION}\n")
    video = root / "video"

    # Sidecar-only video: real speech audio, no embedded subtitle stream.
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=navy:s=160x120:r=10:d=2",
        "-i",
        str(speech),
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-t",
        "2",
        str(video / "lesson.mp4"),
    )
    _write(video / "lesson.en.srt", SIDECAR_SRT)
    _write(video / "lesson.en.vtt", SIDECAR_VTT)
    _write(video / "lesson.tricky.srt", TRICKY_SRT)

    # Embedded caption track: mov_text inside MP4, no sidecar beside it.
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=darkgreen:s=160x120:r=10:d=2",
        "-i",
        str(speech),
        "-i",
        str(embedded_srt),
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-map",
        "2:s",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-c:s",
        "mov_text",
        "-metadata:s:s:0",
        "language=eng",
        "-t",
        "2",
        str(video / "embedded.mp4"),
    )

    # Silent video: no audio and no captions, useful for empty ASR results.
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x120:rate=10:duration=1",
        "-c:v",
        "libvpx",
        "-an",
        str(video / "silent.webm"),
    )

    # Same-stem media for the ambiguous-sidecar scenario.
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=orange:s=64x64:r=5:d=1",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(video / "shared.mp4"),
    )
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=purple:s=64x64:r=5:d=1",
        "-c:v",
        "libvpx",
        "-an",
        str(video / "shared.webm"),
    )
    _write(video / "shared.srt", "1\n00:00:00,000 --> 00:00:01,000\nambiguous zebra\n")


def _build_images(root: Path) -> None:
    _render_text_image(root / "image" / "scan.png")
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "gradients=size=160x120:duration=1",
        "-frames:v",
        "1",
        str(root / "image" / "photo.jpg"),
    )
    _write(
        root / "image" / "diagram.svg",
        '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60">'
        '<rect width="120" height="60" fill="#eef"/>'
        '<text x="8" y="34" font-size="14">diagram</text>'
        "</svg>\n",
    )


def _build_documents(root: Path) -> None:
    documents = root / "documents"
    _write(
        documents / "notes.txt",
        "Native plain text about zebra migration.\nSecond line.\n",
    )
    _write(
        documents / "notes.md",
        "# Native markdown\n\nA paragraph about unicorn geometry.\n",
    )
    _write(
        documents / "page.html",
        "<html><head><title>Native HTML page</title></head><body>"
        "<h1>Native HTML page</h1><p>Body text about algebra and zebra.</p>"
        "</body></html>\n",
    )
    _render_pdf(documents / "text.pdf", "native pdf zebra text layer")
    _render_scanned_pdf(documents / "scanned.pdf", root / "image" / "scan.png")


def _build_other(root: Path) -> None:
    other = root / "other"
    _write(other / "styles.css", "body { color: #123; }\n")
    _write(other / "app.js", "console.log('fixture');\n")
    _write(other / "data.json", '{"fixture": true}\n')
    _write(other / "unknown.bin", bytes(range(64)) + b"\x00\xff\x10\x20")
    with zipfile.ZipFile(other / "archive.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("readme.txt", "archive member\n")


def _manifest() -> dict[str, object]:
    def record(
        path: str,
        family: str | None,
        mimetype: str,
        scenario: str,
    ) -> dict[str, object]:
        return {
            "path": path,
            "media_family": family,
            "mimetype": mimetype,
            "scenario": scenario,
        }

    files = [
        record("audio/speech.mp3", "audio", "audio/mpeg", "speech for ASR"),
        record("audio/speech.ogg", "audio", "audio/ogg", "speech for ASR"),
        record("audio/tone.wav", "audio", "audio/wav", "no speech for ASR"),
        record("video/lesson.mp4", "video", "video/mp4", "sidecar captions + speech"),
        record("video/lesson.en.srt", None, "application/x-subrip", "sidecar SRT"),
        record("video/lesson.en.vtt", None, "text/vtt", "sidecar WebVTT"),
        record("video/lesson.tricky.srt", None, "application/x-subrip", "tags, entities, NOTE"),
        record("video/embedded.mp4", "video", "video/mp4", "embedded mov_text captions + speech"),
        record("video/silent.webm", "video", "video/webm", "no audio, no captions"),
        record("video/shared.mp4", "video", "video/mp4", "ambiguous sidecar target"),
        record("video/shared.webm", "video", "video/webm", "ambiguous sidecar target"),
        record("video/shared.srt", None, "application/x-subrip", "ambiguous sidecar"),
        record("image/scan.png", "image", "image/png", "readable text for OCR"),
        record("image/photo.jpg", "image", "image/jpeg", "no text"),
        record("image/diagram.svg", "image", "image/svg+xml", "vector image"),
        record("documents/notes.txt", "text", "text/plain", "native text"),
        record("documents/notes.md", "text", "text/markdown", "native markdown"),
        record("documents/page.html", "text", "text/html", "native HTML"),
        record("documents/text.pdf", "text", "application/pdf", "native PDF text"),
        record("documents/scanned.pdf", "text", "application/pdf", "image-only PDF for OCR"),
        record("other/styles.css", None, "text/css", "servable asset"),
        record("other/app.js", None, "application/javascript", "servable asset"),
        record("other/data.json", None, "application/json", "servable asset"),
        record("other/unknown.bin", None, "application/octet-stream", "unknown attachment"),
        record("other/archive.zip", None, "application/zip", "attachment"),
    ]
    return {
        "fixture_version": 1,
        "speech": SPEECH,
        "embedded_caption": EMBEDDED_CAPTION,
        "files": files,
    }


def generate(root: Path = ROOT, *, force: bool = False) -> Path:
    root = Path(root)
    if force:
        for name in (
            "audio",
            "video",
            "image",
            "documents",
            "other",
            "manifest.json",
        ):
            path = root / name
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
    work = root / ".work"
    work.mkdir(parents=True, exist_ok=True)
    try:
        _speech_wav(work / "speech.wav")
        _build_audio(root, work)
        _build_video(root, work)
        _build_images(root)
        _build_documents(root)
        _build_other(root)
        _write(
            root / "manifest.json",
            json.dumps(_manifest(), indent=2) + "\n",
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="destination directory (default: this directory)",
    )
    parser.add_argument("--force", action="store_true", help="replace generated files")
    args = parser.parse_args(argv)
    print(generate(args.root, force=args.force).resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
