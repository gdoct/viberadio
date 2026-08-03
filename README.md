# Vibe Radio

An AI-run radio station in the mould of the ones in **Grand Theft Auto**: a dial of
stations, each with a DJ who has a personality, grudges and running jokes, playing
records around banter, listener requests and news on the hour. Everything except
the music is generated — the programming, the talk, the voices — and it runs
continuously, so tuning in at half past three in the morning gets you whatever the
station happens to be doing at half past three in the morning.

The listener experience is a React studio console with live now-playing data, a
spectrum display, station switching, volume control, and a request line.
<img width="1170" height="765" alt="image" src="https://github.com/user-attachments/assets/8bc626b0-9842-4377-909c-779c266b355a" />

## What It Sounds Like

The DJ is not a continuity announcer. Most breaks have nothing to do with the
music: a rant about something small and specific, an overshare about their own
life, a phone call they claim to have just taken, a badly-read ad for a business
that does not exist. Each station's presenter has running obsessions they come
back to weeks apart, a catchphrase they overuse, and the last few breaks fed back
to them so a joke can pay off three records later.

Twice an hour the DJ hands over to the station's newsreader — a second character,
in a second voice — for real headlines off an RSS wire. This actually aired:

```
08:24:54  Kyle:   Marge. What have you got back there.
          Marge:  After this record: John Williams has traded the studio for a
                  coffee shop in Gouda, and says it gives him energy.
          Kyle:   A coffee shop. Fifty-six and he's pulling shots in Gouda.
                  Your best memories were louder than this. Bill Withers, Use Me.
08:25:00  ♪ Bill Withers — Use Me
08:29:43  Marge:  The showbusiness news. John Williams, fifty-six, co-owns a coffee
                  shop in Gouda... And at an airport in Alabama, a security guard
                  found cannonballs in a passenger's suitcase last month, dating
                  from the Civil War. Back to the music.
          Kyle:   Thank you, Marge Kellerman. Cannonballs. In a suitcase. Somebody
                  packed those and thought, yeah, that's fine, that'll fly.
08:29:54  ♪ War — Low Rider
```

The news is on the hour and the gossip on the half hour, each trailed a few
minutes earlier by an exchange like the one above. Hitting :00 and :30 is why the
station plans its day in advance rather than choosing records as it goes.

## How It Works

Each station is run by five cooperating agents:

- **Programmer** decides the day. It writes the running order out to the end of
  tomorrow — one LLM call per hour, choosing from the station's own records — and
  fits every half hour so it ends exactly on its mark, with the bulletin and its
  trail built in.
- **Song selector** handles listener requests and promotes the programme onto the
  playlist as each item's turn comes.
- **Voice segment agent** writes the DJ's banter, records the news handovers in
  two voices, and synthesizes everything with Kokoro TTS. A schedule change only
  goes live when its voice break is ready.
- **Newsroom** polls the RSS wire (at most once an hour, shared across stations)
  and has each station's anchor write that hour's copy in character.
- **Audio engineer** renders the continuous programme ahead of playback, applies
  crossfades and voice ducking, then produces timestamped HLS segments.

However many people speak in a break, it reaches the renderer as a single voice
item that opens over the outgoing record and rides the next one's intro, with the
music ducked underneath — so a two-hander mixes exactly like a one-liner.

All listeners to a station hear the same point in its programme. The HLS playlist
uses program-date-time metadata so the client can show the track actually coming
through its speakers, rather than the backend's render lead.

The dial currently includes:

- **KGOR** - 60s and 70s rock
- **KJFK** - jazz, funk, and classic soul
- **KBON** - 90s alternative, grunge, britpop, and hip hop

Each is one Markdown file in [`backend/stations/`](backend/stations/) holding its
name, style, DJ, persona, running bits, catchphrase, newsreader, and the two
voices. Edit a file to change a station or drop a new one in to add one - no code
changes.

Stations start when somebody tunes in and stop after an idle timeout, keeping
downloads, LLM calls, and synthesis work scoped to active listening. Each station
keeps its own playlist, timeline, and playback history.

They share one **media library** — a track downloaded for one is instantly
available to the others — but not each other's taste. A record becomes a
station's own only when somebody with judgement puts it there: the DJ naming it
while programming an hour, or a listener requesting it and the DJ agreeing it
fits. Rotation, and the fitter's swaps, may only draw on that shelf. Airplay
grants nothing, so one bad hour cannot teach the jazz station that Led Zeppelin
is one of its records.

## The Programme

A station knows what it is playing tomorrow. The programmer writes a running
order out to the end of tomorrow in the station's timezone, in half-hour blocks:

```
|:00|  [BULLETIN]  song  song  song  [TRAIL]  song  |:30|  [BULLETIN]  song ...
        ^ on the mark              ^ trails the next mark, about five minutes out
```

Each block is **cut to the clock**. The DJ hands over a running order; the fitter
fills the half hour from it and then swaps, adds or drops records against the
station's shelf until the projected end of the block lands on the mark. In
practice it gets inside a second or two.

Nothing is fixed once written. DJ breaks are not planned, so a block drifts a
little from its projection; the earliest block that has not been promoted yet is
re-cut against the real timeline cursor, which stops the drift compounding across
the day. A listener request goes in at the head of that block and a rotation
record is dropped to pay for it, so the mark does not move. A station nobody
listened to for three hours rejoins the programme at the present — the day is a
wall-clock grid, not a queue.

News items are recorded fifteen minutes before their airtime, so a bulletin is
never waiting on speech synthesis at its mark. Until it is recorded the block
holds a reservation for it; afterwards the block is re-fitted to the real length.

## Tech Stack

- Python 3.14, FastAPI, SQLAlchemy, and SQLite
- React 19, TypeScript, Vite, and hls.js
- Claude Agent SDK for programme decisions, DJ writing, and news copy
- Kokoro ONNX for local speech synthesis, one voice per presenter
- FFmpeg for audio processing and HLS output
- RSS over the standard library for the news wire

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

Thirty-eight, on this frequency since the last owner sold it, and convinced the
building is trying to get rid of her. Warm to the audience, vicious about the
day-shift. Starts a story about her landlord and finishes it about something
else entirely.

## Bits

Her landlord, who she is certain reads her post. The van outside that has not
moved since March. A demo tape she keeps threatening to play.

## News anchor

Dorian, who reads the news like he is reading a will. He and Maya have not
agreed on anything since the flood.

## Catchphrase

Stay up with the city

## TTS voice

af_bella

## News voice

bm_george
```

`Style`, `DJ`, `Persona`, `Catchphrase` and `TTS voice` are required; `Bits`,
`News anchor` and `News voice` are optional.

- The `#` title is the station name displayed to listeners.
- `Style` is the music brief the DJ programmes from. Include genres, eras, moods,
  and notable boundaries that should guide its choices.
- `DJ` is the presenter's name, used in breaks and by the newsreader on air.
- `Persona` is the presenter's character brief, sent verbatim to the writing
  prompt. Give them an age, a history at the station, a temper, an opinion about
  the audience, something they are wrong about. This is a character, not a
  voice-casting note — timbre is `TTS voice`'s job.
- `Bits` are the running gags: people, grudges and objects the DJ can return to
  weeks apart. The last few breaks are fed back to them, so anything listed here
  can turn into a joke that pays off later instead of a one-off.
- `News anchor` is a second character on the station — the person who reads the
  wire, not the DJ in a different mood. They talk to the DJ twice an hour, so give
  them a relationship to them and an attitude to the job. Left out, the DJ reads
  the news themselves and there is no exchange.
- `Catchphrase` is an occasional recurring line, buried mid-break about once an
  hour.
- `TTS voice` is the Kokoro voice for the DJ, and `News voice` the one for the
  anchor. Use voices available in the downloaded `voices-v1.0.bin` — the bundled
  stations use `am_onyx`/`af_sarah`, `af_bella`/`am_echo` and
  `am_michael`/`bf_isabella`. **The two must differ**: the same voice twice is not
  a conversation. Left out, `News voice` falls back to the `NEWS_TTS_VOICE`
  setting.

Keep the prose sections intentional and listener-facing: the backend passes their
text directly to the agents. The required `##` headings are matched without regard
to case, extra sections are ignored, and each required section must contain text.
A missing title, an empty required section, an invalid file name, or two files
with the same slug prevents the backend from starting.

To change a station, edit its file and restart the backend. Its name, music brief,
DJ details, running bits, newsreader, catchphrase, both voices, and dial position
update while the station keeps its existing playlist, timeline, requests, shelf
and history. To add a station,
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

Everything else is environment configuration. Every field in
`backend/viberadio/config.py` can be overridden in `backend/.env`; copy
`backend/.env.example` and uncomment what you need. Beyond the basics
(`STATION_IDLE_TIMEOUT_SEC`, `LOOKAHEAD_SEC`), the ones worth knowing:

| Setting | What it does |
| --- | --- |
| `STATION_TIMEZONE` | Which day "today and tomorrow" means. The :00 and :30 marks are the same in any whole-hour offset. |
| `PROGRAMME_MARK_TOLERANCE_SEC` | How far off the mark a half hour may land before it is re-cut. Lower is tighter and re-cuts more often. |
| `NEWS_ON_THE_HOUR` | Set false for a station dial with no news at all. |
| `NEWS_SOURCES`, `NEWS_GOSSIP_SOURCES`, `NEWS_REMARKABLE_SOURCES` | The RSS wire, as JSON lists. Defaults to NU.nl; the anchor translates into whatever language they broadcast in. |
| `NEWS_RENDER_LEAD_SEC` | How far ahead the studio records a bulletin. |
| `NEWS_TTS_VOICE` | The anchor's voice on stations whose file does not name one. |

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
