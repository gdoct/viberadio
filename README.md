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

Each is one Markdown file in [`backend/stations/`](backend/stations/) holding its
name, style, DJ, persona, catchphrase, and voice. Edit a file to change a station
or drop a new one in to add one - no code changes.

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

### Start both services

After installing the backend and frontend dependencies above, start both development
servers from the repository root:

```bash
bash ./start.sh
```

Press `Ctrl+C` to stop both services.

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

database, so a name edited there is not overwritten.
The dial is defined by the Markdown files in
[backend/stations/](backend/stations/). The backend reads them and synchronizes
their station settings to the database every time it starts. This makes the files
the source of truth for a station's identity and creative brief.

### Customize stations

Each station is one file named `<order>-<slug>.md`, such as
`01-kgor.md`. The numeric prefix determines its position in the station picker;
renumber files to reorder the dial. A dash is recommended between the order and
slug (an underscore also works). The slug must start with a lowercase letter or
number and may then contain lowercase letters, numbers, and dashes. It is used in
the API and stream URL, for example `/stream/kgor/playlist.m3u8`, and identifies
that station's database row and HLS directory.

Start with this complete template:

```markdown
# Midnight City Radio

## Style

80s new wave, synth-pop, and alternative dance; melodic, nocturnal, and energetic

## DJ

Maya

## Persona

Warm, precise, and lightly mischievous. Speaks like a longtime late-night host
who knows the records and keeps links between songs brief.

## Catchphrase

Stay up with the city

## TTS voice

af_bella
```

All six values are required:

- The `#` title is the station name displayed to listeners.
- `Style` is the music brief given to the selector. Include genres, eras, moods,
  and notable boundaries that should guide its choices.
- `DJ` is the presenter's name used in generated breaks.
- `Persona` is the presenter's voice and writing brief. It is sent verbatim to the
  DJ-writing prompt, so specify tone, pacing, knowledge, and on-air manner.
- `Catchphrase` is an occasional recurring line for DJ breaks.
- `TTS voice` is the Kokoro voice identifier used for that station's DJ. Use a
  voice available in the downloaded `voices-v1.0.bin`; the bundled station
  examples use `am_onyx`, `af_bella`, and `am_michael`.

Keep `Style`, `Persona`, and `Catchphrase` intentional and listener-facing: the
backend passes their text directly to the agents. The required `##` headings are
matched without regard to case, extra sections are ignored, and each required
section must contain text. A missing title, an empty required section, an invalid
file name, or two files with the same slug prevents the backend from starting.

To change a station, edit its file and restart the backend. Its name, music brief,
DJ details, catchphrase, TTS voice, and dial position update while the station
keeps its existing playlist, timeline, requests, and history. To add a station,
create a new file with a new slug, restart the backend, then optionally seed music
suited to its style:

```bash
cd backend
uv run python -m viberadio.bootstrap --seed --station midnight-city
```

Changing only the title is a rename and keeps the station's history. Changing the
slug creates a new station identity; it does not rename or remove the old one.
Deleting a station file likewise stops it from being updated but leaves its
existing database row on the dial. To retire it completely, stop the backend and
remove its row from the backend directory:

```bash
sqlite3 data/viberadio.db "DELETE FROM channels WHERE slug = 'midnight-city'"
```

Files named `README.md` or beginning with `.` or `_` are ignored. For a compact
reference alongside the station definitions, see
[backend/stations/README.md](backend/stations/README.md).

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
