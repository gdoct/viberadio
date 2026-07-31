import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { postRequest } from './api'
import { DJStrip } from './components/DJStrip'
import { Header } from './components/Header'
import { NowPlaying } from './components/NowPlaying'
import { StudioLine } from './components/StudioLine'
import { useRadioAudio } from './hooks/useRadioAudio'
import { useSpectrum } from './hooks/useSpectrum'
import { useChannels, useStation } from './hooks/useStation'
import { airState, djView, nowView, whatIsHeard } from './station-view'
import { buildThread } from './thread'

/** Remember the last station across reloads. */
const STORED_SLUG_KEY = 'viberadio.station'

export default function App() {
  const channels = useChannels()
  const [selected, setSelected] = useState<string | null>(
    () => localStorage.getItem(STORED_SLUG_KEY),
  )

  // Until the dial has loaded we don't know which stations exist; fall back to
  // the first one the backend lists rather than guessing a slug.
  const slug =
    selected && channels.some((c) => c.slug === selected)
      ? selected
      : (channels[0]?.slug ?? null)

  const { station, requests, djLines, error, elapsed, refresh } = useStation(slug)
  const streamUrl = station?.stream_url ?? null
  const audio = useRadioAudio(streamUrl)
  const composerRef = useRef<HTMLInputElement | null>(null)

  // While tuned in, everything is keyed to the moment coming out of the
  // speakers rather than the station's live edge — HLS runs well behind it, and
  // showing the live edge means announcing a track change a listener won't hear
  // for another half-minute.
  const heard = useMemo(
    () => whatIsHeard(station, audio.playbackDate, elapsed, djLines),
    [station, audio.playbackDate, elapsed, djLines],
  )
  const air = airState(heard.airing, station?.status)
  const view = nowView(station, heard)
  const dj = djView(station, heard, air)

  // Real FFT once the listener is tuned in; otherwise the bars run the
  // synthetic waveform whenever the station is on air.
  useSpectrum(audio.analyser, air === 'air' || air === 'mic')

  const heardAtMs = audio.playbackDate?.getTime() ?? null
  const messages = useMemo(
    () => buildThread(station, requests, djLines, dj.name, heardAtMs),
    [station, requests, djLines, dj.name, heardAtMs],
  )

  const selectStation = useCallback((next: string) => {
    setSelected(next)
    localStorage.setItem(STORED_SLUG_KEY, next)
  }, [])

  // Follow the listener across the switch: if they were listening to the old
  // station, tune into the new one as soon as it has something to play.
  const wasTuned = useRef(false)
  useEffect(() => {
    if (audio.tunedIn) wasTuned.current = true
  }, [audio.tunedIn])

  const { retune } = audio
  useEffect(() => {
    if (!streamUrl || !wasTuned.current) return
    retune()
  }, [streamUrl, retune])

  const send = useCallback(
    async (text: string) => {
      if (!slug) return
      await postRequest(slug, text)
      refresh()
    },
    [slug, refresh],
  )

  const focusComposer = useCallback(() => composerRef.current?.focus(), [])

  return (
    <div className="app">
      <Header
        channel={station?.channel ?? null}
        channels={channels}
        selected={slug}
        air={air}
        onSelect={selectStation}
      />

      <main className="body">
        <div className="stack">
          <NowPlaying
            view={view}
            air={air}
            tune={audio.status}
            tunedIn={audio.tunedIn}
            volume={audio.volume}
            onVolume={audio.setVolume}
            onToggle={audio.toggle}
            onRequest={focusComposer}
          />
          <DJStrip
            name={dj.name}
            badge={dj.badge}
            badgeClass={dj.badgeClass}
            status={dj.status}
            air={air}
            listening={audio.tunedIn}
          />
        </div>

        <StudioLine messages={messages} inputRef={composerRef} onSend={send} />
      </main>

      {/* Shown for any failed poll, not just the first: otherwise a backend that
          dies mid-session leaves stale data on screen looking live. */}
      {error && (
        <div className="offline" role="alert">
          {station ? (
            <>Lost the station backend ({error}) — this is the last known state.</>
          ) : (
            <>
              Can’t reach the station backend ({error}). Start it with{' '}
              <code>uv run uvicorn viberadio.main:app</code>.
            </>
          )}
        </div>
      )}

      {/* Driven entirely by useRadioAudio; hidden because the transport row is
          the real control surface. */}
      <audio ref={audio.audioRef} preload="none" />
    </div>
  )
}
