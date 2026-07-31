/**
 * Derives what the panels display.
 *
 * The station reports the entry airing *now*, but HLS always plays some way
 * behind the live edge — so "now" is not what the listener is hearing. When
 * tuned in we resolve the entry that was on air at the playhead's wall-clock
 * position instead, so the screen and the speakers agree.
 */

import type {
  EntryKind,
  StationResponse,
  StationStatus,
  TrackInfo,
} from './api'
import { clock, trackLabel } from './format'
import type { DJLine } from './thread'

/** What the station is doing — independent of whether you're listening.
    `starting` is a cold station whose agents are up but which has nothing on
    air yet: the first records still have to be chosen and downloaded. */
export type AirState = 'air' | 'mic' | 'off' | 'starting'

/** One playlist entry, however it was sourced (live, history, or resolved). */
export interface Airing {
  kind: EntryKind
  track: TrackInfo | null
  voiceScript: string | null
  startedAtMs: number | null
  durationSec: number | null
}

export interface Heard {
  airing: Airing | null
  elapsedSec: number
}

/** Tolerance when matching a captured DJ script to a resolved voice entry. */
const SCRIPT_MATCH_MS = 2500

const ms = (iso: string | null): number | null => {
  if (!iso) return null
  const t = Date.parse(iso)
  return Number.isNaN(t) ? null : t
}

function liveAiring(station: StationResponse | null): Airing | null {
  const now = station?.now_playing
  if (!now) return null
  return {
    kind: now.kind,
    track: now.track,
    voiceScript: now.voice_script,
    startedAtMs: ms(now.started_at),
    durationSec: now.track?.duration_sec ?? null,
  }
}

/** History drops `voice_script`, so recover it from what we captured on air. */
function scriptFor(startedAtMs: number | null, djLines: DJLine[]): string | null {
  if (startedAtMs == null) return null
  const hit = djLines.find((l) => Math.abs(l.at - startedAtMs) <= SCRIPT_MATCH_MS)
  return hit?.text ?? null
}

export function whatIsHeard(
  station: StationResponse | null,
  playbackDate: Date | null,
  liveElapsedSec: number,
  djLines: DJLine[],
): Heard {
  if (!station) return { airing: null, elapsedSec: 0 }
  if (!playbackDate) {
    // Not tuned in: nothing is being heard, so show the station's live state.
    return { airing: liveAiring(station), elapsedSec: liveElapsedSec }
  }

  const at = playbackDate.getTime()
  const candidates: Airing[] = station.history.flatMap((item) => {
    const start = ms(item.actual_start)
    if (start == null) return []
    const end = ms(item.actual_end)
    return [
      {
        kind: item.kind,
        track: item.track,
        voiceScript: null,
        startedAtMs: start,
        // Voice segments carry no duration of their own; the aired window is it.
        durationSec:
          item.track?.duration_sec ?? (end != null ? (end - start) / 1000 : null),
      },
    ]
  })

  const live = liveAiring(station)
  if (live?.startedAtMs != null) candidates.push(live)
  candidates.sort((a, b) => (a.startedAtMs ?? 0) - (b.startedAtMs ?? 0))

  // Entries are contiguous, so what's playing is the latest one that had begun.
  let chosen: Airing | null = null
  for (const candidate of candidates) {
    if ((candidate.startedAtMs ?? 0) > at) break
    chosen = candidate
  }
  // Playhead older than everything we know about — fall back to the oldest.
  if (!chosen) chosen = candidates[0] ?? live ?? null

  if (chosen && chosen.kind === 'voice' && !chosen.voiceScript) {
    chosen = { ...chosen, voiceScript: scriptFor(chosen.startedAtMs, djLines) }
  }

  return {
    airing: chosen,
    elapsedSec:
      chosen?.startedAtMs != null
        ? Math.max(0, (at - chosen.startedAtMs) / 1000)
        : 0,
  }
}

export function airState(
  airing: Airing | null,
  status: StationStatus | undefined,
): AirState {
  if (airing) return airing.kind === 'voice' ? 'mic' : 'air'
  // Nothing on air and the station isn't settled yet — it's warming up, not dead.
  return status && status !== 'live' ? 'starting' : 'off'
}

export interface NowView {
  eyebrow: string
  title: string
  artist: string
  upNext: string
  elapsed: string
  duration: string
  progressPct: number
}

export function nowView(
  station: StationResponse | null,
  heard: Heard,
): NowView {
  const { airing, elapsedSec } = heard
  const air = airState(airing, station?.status)
  const channel = station?.channel

  const next = station?.queue.find((item) => item.kind === 'song' && item.track)
  const upNext = next?.track ? `UP NEXT · ${trackLabel(next.track)}` : ''

  if (air === 'starting') {
    return {
      eyebrow: 'SPINNING UP',
      title: `Waking ${channel?.name ?? 'the station'}…`,
      artist: `${channel?.dj_name ?? 'The DJ'} is picking the first records. This takes a minute on a cold station.`,
      upNext,
      elapsed: '0:00',
      duration: '0:00',
      progressPct: 0,
    }
  }

  if (air === 'off') {
    return {
      eyebrow: 'OFF AIR',
      title: 'The studio is quiet',
      artist: station
        ? 'Waiting for the selector to queue a track'
        : 'Connecting…',
      upNext: '',
      elapsed: '0:00',
      duration: '0:00',
      progressPct: 0,
    }
  }

  const total = airing?.durationSec ?? 0

  if (air === 'mic') {
    return {
      eyebrow: 'DJ BREAK',
      title: `${channel?.dj_name ?? 'The DJ'} is talking…`,
      artist: 'Live voice break · next song cueing',
      upNext,
      elapsed: clock(elapsedSec),
      duration: total > 0 ? clock(total) : '—:—',
      progressPct: total > 0 ? Math.min(100, (elapsedSec / total) * 100) : 0,
    }
  }

  return {
    eyebrow: 'NOW PLAYING',
    title: airing?.track?.title ?? 'Untitled',
    artist: airing?.track?.artist ?? 'Unknown artist',
    upNext,
    elapsed: clock(Math.min(elapsedSec, total || elapsedSec)),
    duration: clock(total),
    // Crossfades mean the next entry starts before this one ends, so elapsed can
    // creep past the track length — clamp instead of overflowing the bar.
    progressPct: total > 0 ? Math.min(100, (elapsedSec / total) * 100) : 0,
  }
}

const DJ_BADGE: Record<AirState, string> = {
  air: 'LIVE',
  mic: 'ON MIC',
  off: 'STANDBY',
  starting: 'WARMING UP',
}

export function djView(
  station: StationResponse | null,
  heard: Heard,
  air: AirState,
) {
  const channel = station?.channel
  const script = heard.airing?.voiceScript?.trim()

  let status: string
  if (air === 'starting')
    status = `getting behind the desk · ${channel?.style ?? 'setting up'}`
  else if (air === 'off') status = 'standing by · ready when you are'
  else if (air === 'mic')
    status = script ? `on the mic · “${script}”` : 'on the mic'
  else status = `in the mix · ${channel?.catchphrase ?? 'curating your set'}`

  return {
    name: channel?.dj_name ?? 'The DJ',
    badge: DJ_BADGE[air],
    badgeClass:
      air === 'air'
        ? 'is-air'
        : air === 'mic'
          ? 'is-mic'
          : air === 'starting'
            ? 'is-starting'
            : 'is-off',
    status,
  }
}
