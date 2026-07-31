/**
 * The studio line is a single chronological log built from three real sources:
 * playback history (what went to air), the listener request board, and the DJ
 * scripts observed while a voice break is playing.
 *
 * On-air events are held back until the listener has actually heard them, so the
 * log never spoils a track change that is still seconds away in their stream.
 * Their own messages are not held back — those happen in real time.
 */

import type {
  HistoryItem,
  RequestInfo,
  RequestStatus,
  StationResponse,
} from './api'
import { trackLabel } from './format'

export type ThreadRole =
  /** Centred system line — a track hitting the air. */
  | 'sys'
  /** The DJ speaking: a voice break, or a verdict on a request. */
  | 'dj'
  /** A listener message the studio has resolved. */
  | 'listener'
  /** A listener message still working its way through the selector. */
  | 'pending'

export interface ThreadMessage {
  id: string
  role: ThreadRole
  who?: string
  text: string
  tag?: string
  /** Epoch millis: airtime for broadcast events, sent-time for listener ones. */
  at: number
  /** Went out over the air, so it waits until the listener has heard it. */
  broadcast: boolean
}

/** A DJ script, stamped with the airtime of the voice entry that carried it. */
export interface DJLine {
  at: number
  text: string
}

const REQUEST_TAGS: Record<RequestStatus, string> = {
  new: 'on the studio line',
  judging: 'the DJ is weighing it up',
  downloading: 'pulling the track',
  done: 'accepted · airs after this song',
  rejected: 'not this one',
}

const RESOLVED: RequestStatus[] = ['done', 'rejected']

const ms = (iso: string | null): number => (iso ? Date.parse(iso) : Number.NaN)

function historyLines(history: HistoryItem[]): ThreadMessage[] {
  return history.flatMap((entry, i) => {
    if (entry.kind !== 'song' || !entry.track) return []
    const at = ms(entry.actual_start)
    if (Number.isNaN(at)) return []
    return [
      {
        id: `played-${entry.track.id}-${at || i}`,
        role: 'sys' as const,
        text: `Now playing · ${trackLabel(entry.track)}`,
        at,
        broadcast: true,
      },
    ]
  })
}

function requestLines(requests: RequestInfo[], djName: string): ThreadMessage[] {
  return requests.flatMap((req) => {
    const at = ms(req.created_at)
    const resolved = RESOLVED.includes(req.status)
    const lines: ThreadMessage[] = [
      {
        id: `req-${req.id}`,
        role: resolved ? 'listener' : 'pending',
        who: req.requester?.trim() || 'You',
        text: req.message,
        tag: REQUEST_TAGS[req.status],
        at,
        broadcast: false,
      },
    ]
    // The selector writes a verdict when it accepts or turns a request down —
    // that is the DJ answering the listener directly, not something that aired,
    // so it appears as soon as the studio decides.
    if (resolved && req.verdict_reason) {
      lines.push({
        id: `req-${req.id}-verdict`,
        role: 'dj',
        who: djName,
        text: req.verdict_reason,
        at: at + 1,
        broadcast: false,
      })
    }
    return lines
  })
}

export function buildThread(
  station: StationResponse | null,
  requests: RequestInfo[],
  djLines: DJLine[],
  djName: string,
  heardAtMs: number | null,
): ThreadMessage[] {
  if (!station) return []

  const messages: ThreadMessage[] = [
    ...historyLines(station.history),
    ...requestLines(requests, djName),
    ...djLines.map((line, i) => ({
      id: `dj-${line.at}-${i}`,
      role: 'dj' as const,
      who: djName,
      text: line.text,
      at: line.at,
      broadcast: true,
    })),
  ]

  const now = station.now_playing
  if (now?.kind === 'song' && now.track) {
    const at = ms(now.started_at)
    messages.push({
      id: `onair-${now.track.id}-${at}`,
      role: 'sys',
      text: `Now playing · ${trackLabel(now.track)}`,
      at: Number.isNaN(at) ? Date.now() : at,
      broadcast: true,
    })
  }

  const seen = new Set<string>()
  return messages
    .filter((m) => !m.broadcast || heardAtMs == null || m.at <= heardAtMs)
    .sort((a, b) => a.at - b.at)
    .filter((m) => (seen.has(m.id) ? false : seen.add(m.id)))
}
