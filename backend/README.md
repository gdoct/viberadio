# Vibe Radio — backend

An AI-run radio station. Three agents keep it on air:

- **Song selector** — honors listener requests first, then picks tracks that fit the
  channel, downloading anything missing from the media library.
- **Voice segment agent** — writes the DJ's banter and speaks it with local TTS. A
  playlist update only goes on air if its voice segment is ready in time; otherwise the
  update is rejected and the selector replans.
- **Audio engineer** — renders one continuous audio timeline ahead of the clock and
  slices it into HLS segments.

Listeners get an HLS stream where wall-clock time maps to a fixed position in the
timeline, so everyone hears the same thing at the same moment.

## Requirements

- Python 3.12+ (developed on 3.14) and [uv](https://docs.astral.sh/uv/)
- `ffmpeg` and `ffprobe` on PATH
- The Claude Code CLI, logged in — the agents call Claude through the Claude Agent SDK
  using your existing subscription. **No API key is needed or used.**

## Setup

```bash
cd backend
uv sync
cp .env.example .env          # optional: customize the channel and DJ

# Kokoro TTS model files (~340 MB, one time)
mkdir -p data/models && cd data/models
curl -LO https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
curl -LO https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
cd ../..

# Optional: download a starter set so the first minutes aren't the station ident
uv run python -m viberadio.bootstrap --seed
```

Storage is SQLite at `data/viberadio.db` — no database server to run.

## Run

```bash
uv run uvicorn viberadio.main:app
```

The station starts empty and fills itself: the selector picks and downloads songs, the
voice agent records the breaks, and the engineer renders ahead of the clock. Until the
first song is ready the stream plays the station ident on a loop, so it never stalls.

## Listen and inspect

```bash
ffplay http://localhost:8000/stream/playlist.m3u8      # or open in any HLS player
curl localhost:8000/api/station                        # now playing, queue, history
curl -X POST localhost:8000/api/requests \
  -H 'content-type: application/json' \
  -d '{"message":"Can you play Black Betty?","name":"Guido"}'
curl localhost:8000/api/requests                       # watch the verdict come back
```

| Endpoint | Purpose |
|---|---|
| `GET /api/station` | Channel, now playing (with elapsed time), queue, recent history |
| `GET /api/queue`, `GET /api/history` | The same lists, paged |
| `POST /api/requests`, `GET /api/requests` | Listener requests and their verdicts |
| `GET /stream/playlist.m3u8` | Live HLS playlist (sliding ~5 min window) |
| `GET /stream/seg{n}.ts` | Individual segments; kept ~30 min so you can pause and catch up |
| `GET /api/health` | Liveness |

## How the timeline works

The renderer produces one unbroken PCM stream and crossfades between tracks in memory,
then cuts that stream into exactly-10-second segments. Segment boundaries and musical
boundaries never interact, so transitions stay seamless no matter where a segment lands.
Segment *n* always covers `epoch + 10n`, and every segment carries an
`EXT-X-PROGRAM-DATE-TIME` tag tying it to real time.

Restarts are handled three ways: if the renderer was still ahead of the clock it resumes
mid-track at the exact sample; if it fell behind it fast-forwards to the present and
marks an `EXT-X-DISCONTINUITY`; a fresh database starts a new epoch.

## Configuration

The dial lives in [`stations/`](stations/), one Markdown file per station — name,
style, DJ, persona, catchphrase and voice. Edit a file and restart to change a
station, add a file to add one; see [`stations/README.md`](stations/README.md).

Everything in `viberadio/config.py` can be overridden in `.env` — crossfade and
DJ-ducking amounts, how far ahead to render, the fallback TTS voice (`tts_voice`,
default `am_onyx`), the agent tick intervals, and `stations_dir`.

## Notes

- Schema changes are applied with `create_all` at startup; there are no migrations yet.
  To reset, delete `data/viberadio.db*`.
- Songs are fetched with yt-dlp for personal use.
