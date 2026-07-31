import Hls from 'hls.js/light'
import { useCallback, useEffect, useRef, useState } from 'react'

export type TuneStatus = 'off' | 'connecting' | 'live' | 'error'

export interface RadioAudio {
  audioRef: React.RefObject<HTMLAudioElement | null>
  status: TuneStatus
  tunedIn: boolean
  volume: number
  setVolume: (value: number) => void
  toggle: () => void
  /** Tear down and reconnect — used when the listener switches station. */
  retune: () => void
  /** Live FFT node, present once the listener has tuned in. */
  analyser: AnalyserNode | null
  /**
   * Wall-clock time of the audio currently coming out of the speakers, derived
   * from the `EXT-X-PROGRAM-DATE-TIME` tags on each segment. Null when not
   * tuned in. HLS always plays some way behind the live edge, so this — not the
   * clock on the wall — is the moment the listener is actually experiencing.
   */
  playbackDate: Date | null
}

/** How often to re-read the playhead's wall-clock position. */
const PLAYHEAD_INTERVAL_MS = 500
/** How long to wait before retrying a station that hasn't published yet. */
const SPINUP_RETRY_MS = 4000

/**
 * Wires the HLS stream to an `<audio>` element and taps it with an AnalyserNode
 * so the spectrum meter shows the audio actually on air.
 *
 * Nothing is fetched until the listener tunes in: browsers require a gesture to
 * start playback anyway, and an idle tab shouldn't pull a live stream.
 */
export function useRadioAudio(streamUrl: string | null): RadioAudio {
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const hlsRef = useRef<Hls | null>(null)
  const ctxRef = useRef<AudioContext | null>(null)
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Read inside stable callbacks so switching station doesn't rebuild them.
  const urlRef = useRef(streamUrl)
  urlRef.current = streamUrl

  const [status, setStatus] = useState<TuneStatus>('off')
  const [volume, setVolumeState] = useState(0.85)
  const [analyser, setAnalyser] = useState<AnalyserNode | null>(null)
  const [playbackDate, setPlaybackDate] = useState<Date | null>(null)

  const setVolume = useCallback((value: number) => {
    setVolumeState(value)
    if (audioRef.current) audioRef.current.volume = value
  }, [])

  useEffect(() => {
    if (audioRef.current) audioRef.current.volume = volume
  }, [volume])

  const disconnect = useCallback(() => {
    if (retryRef.current) {
      clearTimeout(retryRef.current)
      retryRef.current = null
    }
    hlsRef.current?.destroy()
    hlsRef.current = null
    const audio = audioRef.current
    if (audio) {
      audio.pause()
      audio.removeAttribute('src')
      audio.load()
    }
    setPlaybackDate(null)
  }, [])

  const teardown = useCallback(() => {
    disconnect()
    setStatus('off')
  }, [disconnect])

  useEffect(() => teardown, [teardown])

  // Track where the playhead sits in wall-clock terms while tuned in.
  useEffect(() => {
    if (status !== 'live') return
    const read = () => {
      const hls = hlsRef.current
      const audio = audioRef.current
      let next: Date | null = null

      if (hls) {
        next = hls.playingDate ?? null
      } else {
        // Native HLS (Safari): the element exposes the same anchor itself.
        const el = audio as
          | (HTMLAudioElement & { getStartDate?: () => Date })
          | null
        const start = el?.getStartDate?.()
        if (el && start && !Number.isNaN(start.getTime())) {
          next = new Date(start.getTime() + el.currentTime * 1000)
        }
      }

      // `playingDate` is briefly unavailable across fragment boundaries. Keeping
      // the last known position matters: treating that blip as "no playhead"
      // snaps the whole UI to the live edge for a moment, which shows a track
      // change half a minute before the listener can hear it.
      if (next && !Number.isNaN(next.getTime())) setPlaybackDate(next)
    }
    read()
    const timer = setInterval(read, PLAYHEAD_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [status])

  /**
   * Taps the element for the spectrum meter.
   *
   * `createMediaElementSource` reroutes *all* of the element's audio through the
   * Web Audio graph, permanently and irreversibly. If the AudioContext is
   * suspended — which stricter autoplay policies (Edge) do even for a context
   * created during a click, while Chrome starts it running — the radio plays
   * silently forever: progress advances, nothing comes out.
   *
   * So the context must be proven running before the element is touched, and a
   * failure here costs only the meter, never the audio.
   */
  const attachAnalyser = useCallback(async (audio: HTMLAudioElement) => {
    const existing = ctxRef.current
    if (existing) {
      if (existing.state !== 'running') await existing.resume().catch(() => {})
      return
    }

    const Ctx: typeof AudioContext | undefined =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: typeof AudioContext })
        .webkitAudioContext
    if (!Ctx) return

    let ctx: AudioContext | undefined
    try {
      ctx = new Ctx()
      await ctx.resume().catch(() => {})
      if (ctx.state !== 'running') throw new Error(`context ${ctx.state}`)

      const node = ctx.createAnalyser()
      node.fftSize = 128
      node.smoothingTimeConstant = 0.75
      ctx.createMediaElementSource(audio).connect(node)
      node.connect(ctx.destination)
      ctxRef.current = ctx
      setAnalyser(node)
    } catch (err) {
      // Leave the element untapped and playing straight to the speakers; the
      // meter falls back to its synthetic waveform.
      console.warn('spectrum tap unavailable, audio untouched', err)
      await ctx?.close().catch(() => {})
    }
  }, [])

  // Read through a ref so `connect` stays stable across volume changes and
  // station switches.
  const volumeRef = useRef(volume)
  volumeRef.current = volume

  const connect = useCallback(() => {
    const audio = audioRef.current
    const url = urlRef.current
    if (!audio || !url) return
    setStatus('connecting')
    audio.volume = volumeRef.current

    const play = () => {
      audio
        .play()
        .then(() => {
          setStatus('live')
          // Only tap once sound is confirmed flowing — see attachAnalyser.
          void attachAnalyser(audio)
        })
        .catch(() => setStatus('error'))
    }

    // A station that is still spinning up serves 503 for its playlist until the
    // first segments land. That's expected, not an error — keep knocking.
    const retryLater = () => {
      disconnect()
      setStatus('connecting')
      retryRef.current = setTimeout(() => connect(), SPINUP_RETRY_MS)
    }

    if (Hls.isSupported()) {
      const hls = new Hls({ liveSyncDurationCount: 3, enableWorker: true })
      hlsRef.current = hls
      hls.loadSource(url)
      hls.attachMedia(audio)
      hls.on(Hls.Events.MANIFEST_PARSED, play)
      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (!data.fatal) return
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) retryLater()
        else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError()
        else setStatus('error')
      })
    } else if (audio.canPlayType('application/vnd.apple.mpegurl')) {
      // Safari plays HLS natively; hls.js reports itself unsupported there.
      audio.src = url
      audio.addEventListener('error', retryLater, { once: true })
      play()
    } else {
      setStatus('error')
    }
  }, [attachAnalyser, disconnect])

  const toggle = useCallback(() => {
    if (status === 'off' || status === 'error') connect()
    else teardown()
  }, [connect, status, teardown])

  const retune = useCallback(() => {
    disconnect()
    connect()
  }, [connect, disconnect])

  return {
    audioRef,
    status,
    tunedIn: status === 'connecting' || status === 'live',
    volume,
    setVolume,
    toggle,
    retune,
    analyser,
    playbackDate,
  }
}
