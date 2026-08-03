# Vibe Radio — backend

An AI-run radio station. Four agents keep it on air:

- **Programmer** — decides the day. It writes the running order out to the end of
  tomorrow, an hour at a time, and fits every half-hour so it ends on the mark at
  :00 and :30. See [the programme](#the-programme) below.
- **Song selector** — honors listener requests first, then promotes the next records
  off the programme onto the playlist.
- **Voice segment agent** — writes the DJ's banter and speaks it with local TTS. A
  playlist update only goes on air if its voice segment is ready in time; otherwise the
  update is rejected and the selector replans.
- **Audio engineer** — renders one continuous audio timeline ahead of the clock and
  slices it into HLS segments.

A fourth, the **newsroom**, polls a set of RSS feeds (at most once an hour, shared by
every station) and has each station's news anchor write four pieces of copy off them:
a teaser and a bulletin for the news, and the same pair for gossip. It runs only while
its station is up. What happens to that copy is [on the hour](#on-the-hour) below.

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

The station starts empty and fills itself: the programmer lays out the day, the selector
promotes it and honors requests, the voice agent records the breaks, and the engineer
renders ahead of the clock. Until the first song is ready the stream plays the station
ident on a loop, so it never stalls.

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
| `GET /stream/seg{n}.ts` | Individual segments; kept 120 min so you can pause and catch up |
| `GET /api/health` | Liveness |

## The programme

A station knows what it is playing tomorrow. The programmer writes `programme_slots`
— songs only, in order, with a projected airtime each — out to the end of tomorrow in
the station's timezone, and keeps every half-hour block fitted so it ends on its mark.
That is what makes it possible to put anything at :00 or :30.

Each hour is programmed in one call: the DJ is shown the library and what has aired
recently, and returns a running order plus a couple of records they want that the
library does not have (which is what grows the library now that songs are not picked
one at a time). The order is then **cut to the clock** — the fitter fills the block
from the head and swaps, adds or drops against the rest of the library until the
projected end of the block lands on the mark. It gets inside a second or two in
practice; `programme_mark_tolerance_sec` is the threshold at which it complains.

The first hours of a cold station are filled by rotation instead — a station coming
on air needs records now, not an LLM call — and everything past
`programme_min_hours_ahead` is programmed properly.

Nothing is fixed once written. DJ breaks are not planned, so a block always drifts a
little from its projection; the earliest block that has not been promoted yet is
re-cut against the real timeline cursor every tick, which is what stops the drift
compounding across the day. A listener request goes in at the head of that same block
and a rotation record is dropped to pay for it, so the mark does not move. A station
nobody listened to for three hours rejoins the programme at the present — the day is
a wall-clock grid, not a queue, and what it missed is marked skipped.

## On the hour

Every mark carries a bulletin — the news on the hour, the gossip on the half hour —
and every bulletin is trailed a few minutes earlier by an exchange between the DJ and
the anchor:

```
:55   Kyle:   Marge. What've you got in there.
      Marge:  Coming up on the bulletin: ...
      Kyle:   A hundred and fifty years and no fine. Deborah, take notes.
              Fleetwood Mac, Go Your Own Way.
      [record]
:00   Marge:  [the bulletin]
      Kyle:   Thank you, Marge Kellerman. A man kept a library book a
              century and a half...
      [record] ...
```

Both are `programme_slots` like any record, so the half-hour is fitted around them:
the bulletin opens the block on its mark, the trail sits before the block's last
record, and the fitter puts a four-minute record there so the trail lands at about
:55. The anchor's words are the copy the newsroom already wrote; one LLM call per
mark writes the DJ's three lines around it.

A two-hander is still **one item on the playlist**. The turns are spoken in their own
voices and joined before the voice chain runs, so what the renderer gets is a single
voice file that opens over the outgoing record and rides the next one's intro, exactly
like an ordinary break. Each station names its anchor's voice with `## News voice`.

The studio records each item `news_render_lead_sec` (15 minutes) before its airtime,
so a bulletin is never waiting on Kokoro at its mark. Until it is recorded the block
holds a reservation for it; once it is, the block is re-fitted to the real length. An
item that somehow never got recorded is dropped and a record covers the hole — the
mark matters more than the bulletin.

## How the timeline works

The renderer produces one unbroken PCM stream and crossfades between tracks in memory,
then cuts that stream into exactly-10-second segments. Segment boundaries and musical
boundaries never interact, so transitions stay seamless no matter where a segment lands.
Segment *n* always covers `epoch + 10n`, and every segment carries an
`EXT-X-PROGRAM-DATE-TIME` tag tying it to real time.

Restarts are handled three ways: if the renderer was still ahead of the clock it resumes
mid-track at the exact sample; if it fell behind it fast-forwards to the present and
marks an `EXT-X-DISCONTINUITY`; a fresh database starts a new epoch.

## Housekeeping

A station can be listened back `listen_back_sec` (120 min by default); segment files
older than that are unreachable, and a janitor loop deletes them along with their rows.
It also drops the rendered audio of DJ breaks once they have finished broadcasting —
the words are already mixed into the segments by then, and the script stays in the
database for the history and for writing the next break against. Breaks that were
spoken for a transition that never aired are removed once they age out of the same
window, as are files on disk that no row accounts for after a crash.

The janitor runs process-wide, not per station: stations shut down when nobody is
listening, but their segments outlive them.

## Configuration

The dial lives in [`stations/`](stations/), one Markdown file per station — name,
style, DJ, persona, news anchor, catchphrase and voice. Edit a file and restart to change a
station, add a file to add one; see [`stations/README.md`](stations/README.md).

Everything in `viberadio/config.py` can be overridden in `.env` — crossfade and
DJ-ducking amounts, how far ahead to render, the fallback TTS voice (`tts_voice`,
default `am_onyx`), the agent tick intervals, `stations_dir`, and the news feeds
(`news_sources`, `news_gossip_sources`, `news_remarkable_sources` — JSON lists in
`.env`, defaulting to NU.nl, polled no more than once an hour each).

## Notes

- Schema changes are applied with `create_all` at startup; there are no migrations yet.
  To reset, delete `data/viberadio.db*`.
- Songs are fetched with yt-dlp for personal use.
