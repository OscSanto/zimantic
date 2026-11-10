# Media test fixtures

These files cover supported media families and optional text-extraction
scenarios. Tests use them directly and do not require `ffmpeg`, `espeak-ng`,
or a media generator.

Run the generator to write the fixture tree:

```bash
python tests/fixtures/media/generate.py --force
```

The generator produces a consistent file layout, but not identical bytes.
Media encoders add version metadata. `manifest.json` records each file's media
family, MIME type, and scenario.

## Families

| Family | Files | Notes |
|---|---|---|
| Video | `video/lesson.mp4`, `video/embedded.mp4`, `video/silent.webm`, `video/shared.mp4`, `video/shared.webm` | H.264/AAC MP4, VP8 WebM, with and without audio |
| Audio | `audio/speech.mp3`, `audio/speech.ogg`, `audio/tone.wav` | speech for ASR and a non-speech tone |
| Image | `image/scan.png`, `image/photo.jpg`, `image/diagram.svg` | OCR-readable text, no text, vector image |
| Text | `documents/notes.txt`, `notes.md`, `page.html`, `text.pdf`, `scanned.pdf` | native text, native PDF text layer, image-only PDF |
| Other | `other/styles.css`, `app.js`, `data.json`, `archive.zip`, `unknown.bin` | servable assets and an unknown attachment |

## Text-derived scenarios

| Scenario | Files | Expected behavior |
|---|---|---|
| Sidecar SubRip | `video/lesson.mp4` + `video/lesson.en.srt` | caption text is indexed when `captions = "index"` |
| Sidecar WebVTT | `video/lesson.mp4` + `video/lesson.en.vtt` | `WEBVTT`, `NOTE`, timestamps, and cue indices are skipped |
| Malformed/tricky caption | `video/lesson.tricky.srt` | HTML tags are stripped and entities are unescaped |
| Embedded caption track | `video/embedded.mp4` | one `mov_text` subtitle stream; needs the ffmpeg adapter |
| Captions suppress ASR | `video/lesson.mp4`, `video/embedded.mp4` | usable captions win; ASR is not requested for that media |
| Speech for ASR | `video/lesson.mp4`, `audio/speech.mp3`, `audio/speech.ogg` | roughly "The quick brown fox jumps over the lazy dog." |
| No speech | `audio/tone.wav`, `video/silent.webm` | ASR has nothing useful to transcribe |
| OCR image | `image/scan.png` | Tesseract can read `zebra OCR fixture` |
| OCR image-only PDF | `documents/scanned.pdf` | no text layer, one embedded image |
| Native PDF text | `documents/text.pdf` | selectable `native pdf zebra text layer` |
| Ambiguous sidecar | `video/shared.mp4` + `video/shared.webm` + `video/shared.srt` | reported, attached to neither file |

The sidecar source that muxes `video/embedded.mp4` lives in the generator, not
beside the video, so the embedded scenario does not accidentally become a
sidecar scenario.
