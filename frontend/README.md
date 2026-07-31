# Vibe Radio — frontend

The listener-facing studio dashboard: what's on air right now, what the DJ is
saying, and the studio line where listeners request songs.

React + TypeScript + Vite, with [hls.js](https://github.com/video-dev/hls.js)
for the live stream. The layout is an implementation of the "Late-night console"
design direction (Claude Design project `80361ce2`, `My Radio Studio.dc.html`).

## Running it

The backend must be up first — it serves both the JSON API and the HLS stream:

```bash
cd ../backend && uv run uvicorn viberadio.main:app
```

Then:

```bash
npm install
npm run dev          # http://localhost:5173
```

`npm run build` type-checks and produces `dist/`; `npm run lint` runs oxlint.

Vite proxies `/api` and `/stream` to `http://localhost:8000`. Point that
elsewhere with `VITE_BACKEND=http://host:port npm run dev`. The proxy is not
just convenience: the spectrum meter taps the audio element with an
`AnalyserNode`, and a cross-origin media element reads back as silence.

## How it maps to the backend

Everything on screen comes from the station; nothing is simulated except the
idle meter.

| Panel | Source |
| --- | --- |
| Station dropdown | `GET /api/channels` (name, style, live/starting/off) |
| Status pill, now playing, progress | `GET /api/station?channel=<slug>` → `now_playing` |
| Up next | `GET /api/station` → first song in `queue` |
| Studio line — track lines | `GET /api/station` → `history` |
| Studio line — DJ bubbles | `now_playing.voice_script`, captured as it airs |
| Studio line — listener bubbles | `GET /api/requests?channel=<slug>` |
| Sending a request | `POST /api/requests` (slug in the body) |
| Audio + spectrum | `GET /stream/<slug>/playlist.m3u8` |

## Stations

Three stations share the dial, each with its own agents, timeline, HLS stream
and DJ voice. Everything in the UI is keyed to the selected slug, and switching
clears the previous station's state rather than letting its queue and studio
line bleed into the new one.

Stations run **on demand**: a cold one starts when you select it and shuts down
after `station_idle_timeout_sec` without a listener, so two stations you aren't
listening to don't burn LLM calls and downloads. Selecting a cold station shows
a `SPINNING UP` state while its DJ chooses and downloads the first records —
expect roughly a minute. The stream itself comes up within seconds (the station
ident loops until real programme is scheduled), so `starting` means "nothing on
air yet", not "no stream".

Three details worth knowing:

- **The spectrum tap must never cost you the audio.**
  `createMediaElementSource` reroutes the element's entire output through the
  Web Audio graph, permanently. If the `AudioContext` is suspended — Chrome
  starts it running when created during a click, Edge's stricter autoplay policy
  does not — the radio plays silently: the progress bar advances and nothing
  comes out. `useRadioAudio` therefore starts playback first, and only taps the
  element once the context is confirmed `running`. If it isn't, the element is
  left untouched and the meter falls back to its synthetic waveform.

- **Elapsed time** is interpolated from the `elapsed_sec` the backend reported
  at its last response, not from `started_at`. Only durations need to agree
  between browser and server that way — not wall clocks.
- **DJ scripts** are only exposed while the voice entry is the playing one, so
  `useStation` records each one as it goes to air. History can't recover them
  after the fact.

## The panels follow your ears, not the wall clock

HLS always plays some way behind the live edge — with 10s segments and
`liveSyncDurationCount: 3`, about 25 seconds. `GET /api/station` reports the
entry airing *now*, so rendering it directly would announce a track change well
before the listener could hear it.

Instead, while tuned in, `hls.playingDate` gives the wall-clock time of the
audio currently leaving the speakers (derived from the `EXT-X-PROGRAM-DATE-TIME`
tag the backend writes on every segment). `whatIsHeard()` resolves that instant
against `history` + `now_playing` to find the entry that was on air then, and
every panel is keyed to it. On-air lines in the studio line are held back the
same way; your own messages are not, because those happen in real time.

When you are not tuned in there is no playhead, so the panels show the station's
live state and the meters dim.

Two traps worth remembering:

- `hls.playingDate` is briefly null across fragment boundaries. Treating that as
  "no playhead" snaps everything to the live edge for a moment and flashes the
  next track early — keep the last known position instead.
- This only works because the backend stops broadcasting its render lead; see
  `write_playlist` in `backend/viberadio/audio/hls.py`.

## Layout

```
src/
  api.ts             types + fetch helpers for the backend
  station-view.ts    station response → what the panels display
  thread.ts          history + requests + DJ scripts → one studio log
  format.ts          clock / label helpers
  hooks/
    useStation.ts    polls the station and request board
    useRadioAudio.ts hls.js wiring, transport, AnalyserNode tap
    useSpectrum.ts   drives every meter at 60fps
  components/        Header, NowPlaying, Spectrum, DJStrip, StudioLine
  styles.css         design tokens and layout
```

## Differences from the mockup

The mockup showed transport controls the station has no concept of. Rather than
leave dead buttons on screen, they were dropped:

- **Like / dislike / skip** — there is no listener-preference or skip API.
- **Swap DJ / Start a show** — the DJ persona is channel configuration.

They were replaced by the control the mockup was missing: **tune in / pause and
volume**. Browsers require a gesture before audio can start, so a live stream
needs a play control regardless.

The square "ALBUM ART" plate became a vinyl platter. No track in this system
carries cover art, and a disc is the one shape that can spin without sweeping
outside its own box — the mockup's square visibly overflowed while rotating.
