/** Types and fetch helpers for the Vibe Radio backend (see backend/viberadio/schemas.py). */

export interface ChannelInfo {
  slug: string
  name: string
  style: string
  dj_name: string
  dj_persona: string
  catchphrase: string
}

export interface TrackInfo {
  id: number
  title: string
  artist: string | null
  duration_sec: number
}

/** `kind` mirrors the playlist entry kind: a song, or a DJ voice break. */
export type EntryKind = 'song' | 'voice'

export interface NowPlaying {
  kind: EntryKind
  track: TrackInfo | null
  voice_script: string | null
  started_at: string | null
  elapsed_sec: number | null
}

export interface QueueItem {
  kind: EntryKind
  track: TrackInfo | null
  planned_start: string | null
}

export interface HistoryItem {
  kind: EntryKind
  track: TrackInfo | null
  actual_start: string | null
  actual_end: string | null
}

/** Whether a station's agents are running: cold, waking, or on the air. */
export type StationStatus = 'off' | 'starting' | 'live'

/** One entry on the dial, as listed by `GET /api/channels`. */
export interface ChannelSummary {
  slug: string
  name: string
  style: string
  dj_name: string
  status: StationStatus
}

export interface StationResponse {
  channel: ChannelInfo
  status: StationStatus
  now_playing: NowPlaying | null
  queue: QueueItem[]
  history: HistoryItem[]
  stream_url: string
}

/** The selector agent moves a request through these states. */
export type RequestStatus =
  | 'new'
  | 'judging'
  | 'downloading'
  | 'done'
  | 'rejected'

export interface RequestInfo {
  id: number
  message: string
  requester: string | null
  status: RequestStatus
  verdict_reason: string | null
  created_at: string
}

async function getJSON<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { signal })
  if (!res.ok) throw new Error(`${path} → ${res.status}`)
  return (await res.json()) as T
}

export const fetchChannels = (signal?: AbortSignal) =>
  getJSON<ChannelSummary[]>('/api/channels', signal)

export const fetchStation = (slug: string, signal?: AbortSignal) =>
  getJSON<StationResponse>(`/api/station?channel=${encodeURIComponent(slug)}`, signal)

export const fetchRequests = (slug: string, signal?: AbortSignal) =>
  getJSON<RequestInfo[]>(
    `/api/requests?limit=40&channel=${encodeURIComponent(slug)}`,
    signal,
  )

export async function postRequest(
  slug: string,
  message: string,
  name?: string,
): Promise<RequestInfo> {
  const res = await fetch('/api/requests', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, name: name ?? null, channel: slug }),
  })
  if (!res.ok) throw new Error(`request rejected → ${res.status}`)
  return (await res.json()) as RequestInfo
}
