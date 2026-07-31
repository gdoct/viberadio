import { useCallback, useEffect, useRef, useState } from 'react'

import {
  fetchChannels,
  fetchRequests,
  fetchStation,
  type ChannelSummary,
  type RequestInfo,
  type StationResponse,
} from '../api'
import type { DJLine } from '../thread'

const STATION_INTERVAL_MS = 2000
const REQUESTS_INTERVAL_MS = 4000
const CHANNELS_INTERVAL_MS = 5000
/** Enough studio line history to scroll through without growing unbounded. */
const MAX_DJ_LINES = 40

export interface Station {
  station: StationResponse | null
  requests: RequestInfo[]
  djLines: DJLine[]
  error: string | null
  /** Seconds into the current entry, interpolated between polls. */
  elapsed: number
  /** Re-poll immediately — used after posting a request. */
  refresh: () => void
}

/**
 * Polls one station and its request board.
 *
 * Everything is keyed to `slug`: switching stations tears the state down rather
 * than letting the previous station's queue and studio line bleed into the new
 * one while its first response is in flight.
 *
 * The backend reports `elapsed_sec` as of the moment it answered, so we add the
 * time since that response rather than trusting the browser clock to agree with
 * the server's — the two only need to agree on *durations*, not on wall time.
 */
export function useStation(slug: string | null): Station {
  const [station, setStation] = useState<StationResponse | null>(null)
  const [requests, setRequests] = useState<RequestInfo[]>([])
  const [djLines, setDjLines] = useState<DJLine[]>([])
  const [error, setError] = useState<string | null>(null)
  const [elapsed, setElapsed] = useState(0)

  const baseline = useRef({ elapsed: 0, at: 0 })
  const lastScript = useRef<string | null>(null)
  const [nonce, setNonce] = useState(0)

  const refresh = useCallback(() => setNonce((n) => n + 1), [])

  useEffect(() => {
    if (!slug) return
    // Drop the previous station's view immediately; polling repopulates it.
    setStation(null)
    setRequests([])
    setDjLines([])
    setElapsed(0)
    baseline.current = { elapsed: 0, at: 0 }
    lastScript.current = null

    const controller = new AbortController()
    let cancelled = false

    const poll = async () => {
      try {
        const next = await fetchStation(slug, controller.signal)
        if (cancelled) return
        setStation(next)
        setError(null)
        baseline.current = {
          elapsed: next.now_playing?.elapsed_sec ?? 0,
          at: performance.now(),
        }
        // Capture each DJ break as it goes to air: the script is only exposed
        // while the voice entry is the playing one, so history can't recover it.
        // Stamp it with the entry's airtime, not the time we noticed — that is
        // what lets a script be matched back to its entry once it's history.
        const script = next.now_playing?.voice_script?.trim()
        if (next.now_playing?.kind === 'voice' && script) {
          if (script !== lastScript.current) {
            lastScript.current = script
            const startedAt = Date.parse(next.now_playing.started_at ?? '')
            const at = Number.isNaN(startedAt) ? Date.now() : startedAt
            setDjLines((lines) =>
              [...lines, { at, text: script }].slice(-MAX_DJ_LINES),
            )
          }
        } else if (next.now_playing?.kind === 'song') {
          lastScript.current = null
        }
      } catch (err) {
        if (cancelled || controller.signal.aborted) return
        setError(err instanceof Error ? err.message : 'station unreachable')
      }
    }

    void poll()
    const timer = setInterval(poll, STATION_INTERVAL_MS)
    return () => {
      cancelled = true
      controller.abort()
      clearInterval(timer)
    }
  }, [slug, nonce])

  useEffect(() => {
    if (!slug) return
    const controller = new AbortController()
    let cancelled = false

    const poll = async () => {
      try {
        const rows = await fetchRequests(slug, controller.signal)
        if (!cancelled) setRequests(rows)
      } catch {
        // The station poll already surfaces connectivity problems.
      }
    }

    void poll()
    const timer = setInterval(poll, REQUESTS_INTERVAL_MS)
    return () => {
      cancelled = true
      controller.abort()
      clearInterval(timer)
    }
  }, [slug, nonce])

  useEffect(() => {
    const tick = () => {
      const { elapsed: base, at } = baseline.current
      setElapsed(at ? base + (performance.now() - at) / 1000 : 0)
    }
    tick()
    const timer = setInterval(tick, 250)
    return () => clearInterval(timer)
  }, [])

  return { station, requests, djLines, error, elapsed, refresh }
}

/** The dial, with each station's live/starting/off status. */
export function useChannels(): ChannelSummary[] {
  const [channels, setChannels] = useState<ChannelSummary[]>([])

  useEffect(() => {
    const controller = new AbortController()
    let cancelled = false

    const poll = async () => {
      try {
        const rows = await fetchChannels(controller.signal)
        if (!cancelled) setChannels(rows)
      } catch {
        // Non-fatal: the dropdown keeps showing the last known dial.
      }
    }

    void poll()
    const timer = setInterval(poll, CHANNELS_INTERVAL_MS)
    return () => {
      cancelled = true
      controller.abort()
      clearInterval(timer)
    }
  }, [])

  return channels
}
