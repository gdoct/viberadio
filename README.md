# Vibe Radio

An AI-run radio station that keeps a real-time music programme on air. Vibe Radio
plans songs around listener requests, creates DJ breaks with local text-to-speech,
renders seamless transitions ahead of the clock, and delivers a shared live HLS
stream to every listener.

The listener experience is a React studio console with live now-playing data, a
spectrum display, station switching, volume control, and a request line.
<img width="1170" height="765" alt="image" src="https://github.com/user-attachments/assets/8bc626b0-9842-4377-909c-779c266b355a" />

## How It Works

Each station is operated by three cooperating agents:

- **Song selector** prioritizes listener requests, selects music that matches the
  station, and downloads tracks missing from the media library.
- **Voice segment agent** writes DJ banter and synthesizes it with Kokoro TTS. A
  schedule change only goes live when its voice break is ready.
- **Audio engineer** renders the continuous programme ahead of playback, applies
  crossfades and voice ducking, then produces timestamped HLS segments.

All listeners to a station hear the same point in its programme. The HLS playlist
uses program-date-time metadata so the client can show the track actually coming
through its speakers, rather than the backend's render lead.

The dial currently includes:

- **KGOR** - 60s and 70s rock
- **KJFK** - jazz, funk, and classic soul
- **KBON** - 90s alternative, grunge, britpop, and hip hop

Stations start when somebody tunes in and stop after an idle timeout, keeping
downloads, LLM calls, and synthesis work scoped to active listening. Each station
keeps its own playlist, timeline, and playback history, but they share one media
library, so a track fetched for one is already on hand for the others.

## Tech Stack

- Python 3.14, FastAPI, SQLAlchemy, and SQLite
- React 19, TypeScript, Vite, and hls.js
- Claude Agent SDK for programme decisions
- Kokoro ONNX for local speech synthesis
- FFmpeg for audio processing and HLS output

## Quick Start

### Prerequisites

- Python 3.14+ and [uv](https://docs.astral.sh/uv/)
- Node.js and npm
- `ffmpeg` and `ffprobe` available on your `PATH`
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and logged
  in. The agents use your existing Claude Code subscription; no API key is needed.

### Start the backend

```bash
cd backend
uv sync

# Download the Kokoro voice data once (about 340 MB).
mkdir -p data/models
curl -L -o data/models/kokoro-v1.0.onnx \
  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
curl -L -o data/models/voices-v1.0.bin \
  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin

# Optional: download a starter set for every station on the dial. Add
# --station <slug> to seed just one, or --count N to change how many.
uv run python -m viberadio.bootstrap --seed

uv run uvicorn viberadio.main:app --reload
```

The API and HLS stream are available at `http://localhost:8000`. On a new station,
the ident plays while the agents plan and prepare its first programme.

### Start the frontend

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`, choose a station, and tune in. The Vite development
server proxies `/api` and `/stream` to the local backend. Set `VITE_BACKEND` to use
a backend at another address.

## API At A Glance

| Endpoint | Purpose |
| --- | --- |
| `GET /api/channels` | List available stations and their status |
| `GET /api/station?channel=<slug>` | Get a station's now-playing item, queue, and history |
| `POST /api/requests` | Send a listener song request |
| `GET /api/requests?channel=<slug>` | Read request status and verdicts |
| `GET /api/history?channel=<slug>` | Page back through what has aired |
| `GET /api/queue?channel=<slug>` | Read the full upcoming queue |
| `GET /stream/<slug>/playlist.m3u8` | Play the station's live HLS stream |
| `GET /api/health` | Check backend liveness and which stations are running |

Requesting a station's state or its stream is what counts as tuning in, so both
wake an idle station and keep a running one alive.

For example, listen directly with an HLS-capable player:

```bash
ffplay http://localhost:8000/stream/kgor/playlist.m3u8
```

A cold station answers the first request with `503` while it wakes. Retry after a
few seconds, or open it in the console first and watch it spin up.

## Repository Layout

```text
backend/    FastAPI service, station agents, audio renderer, TTS, and HLS delivery
frontend/   React listener console
spec/       System design and agent workflow documentation
```

Detailed component documentation is available in
[backend/README.md](backend/README.md), [frontend/README.md](frontend/README.md),
and [spec/README.md](spec/README.md).

## Configuration

The dial itself lives in code, in `STATIONS` in
[backend/viberadio/config.py](backend/viberadio/config.py). Each entry sets the
slug used in URLs, the station name and style, the DJ's name, persona, and
catchphrase, and the Kokoro voice that speaks the breaks. Adding an entry creates
that station on the next start; existing stations keep whatever is already in the
database, so a name edited there is not overwritten.

Everything else is environment configuration. Copy `backend/.env.example` to
`backend/.env` and uncomment what you need, such as `STATION_IDLE_TIMEOUT_SEC` to
change how long a station keeps running with no listeners, or `LOOKAHEAD_SEC` to
give a slow machine a longer render lead.

## Development Commands

```bash
# Backend checks
cd backend
uvx ruff check viberadio/
uvx ruff format viberadio/

# Frontend checks
cd frontend
npm run lint
npm run build
```

Runtime data, including the SQLite database, downloaded media, generated speech,
and HLS segments, lives under `backend/data/`. To reset local station state, stop
the backend and remove `backend/data/viberadio.db*`. That also clears the media
library index, so previously downloaded files in `backend/data/media/` are
re-downloaded on demand; delete that directory too for a fully clean start.
