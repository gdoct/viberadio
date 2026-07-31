import type { TuneStatus } from '../hooks/useRadioAudio'
import type { AirState, NowView } from '../station-view'
import { Spectrum } from './Spectrum'

const TUNE_NOTE: Record<TuneStatus, string> = {
  off: 'tune in to listen',
  connecting: 'buffering…',
  live: 'live · you are tuned in',
  error: 'stream unavailable',
}

function tuneNote(tune: TuneStatus, air: AirState): string {
  // While a cold station warms up the stream 503s and we keep retrying, which
  // is not the same thing as buffering a stream that exists.
  if (air === 'starting' && tune === 'connecting') return 'waiting for the station…'
  return TUNE_NOTE[tune]
}

interface Props {
  view: NowView
  air: AirState
  tune: TuneStatus
  tunedIn: boolean
  volume: number
  onVolume: (value: number) => void
  onToggle: () => void
  onRequest: () => void
}

export function NowPlaying({
  view,
  air,
  tune,
  tunedIn,
  volume,
  onVolume,
  onToggle,
  onRequest,
}: Props) {
  const onAir = air === 'air' || air === 'mic'
  const eyebrowClass =
    air === 'mic'
      ? 'is-mic'
      : air === 'air'
        ? 'is-air'
        : air === 'starting'
          ? 'is-starting'
          : ''

  return (
    <section className="panel now">
      <div className="now-head">
        <div
          className={`art${air === 'air' ? ' is-spinning' : ''}`}
          aria-hidden="true"
        >
          <div className={`art-hub${air === 'mic' ? '' : ' is-idle'}`}>
            {air === 'mic' ? '🎙' : ''}
          </div>
        </div>
        <div className="now-meta">
          <div className={`eyebrow ${eyebrowClass}`}>{view.eyebrow}</div>
          <h1 className="track-title">{view.title}</h1>
          <div className="track-artist">{view.artist}</div>
          <div className="up-next">{view.upNext}</div>
        </div>
      </div>

      <Spectrum onAir={onAir} listening={tunedIn} />

      <div className="progress-row">
        <span className="progress-time">{view.elapsed}</span>
        <div
          className="progress-track"
          role="progressbar"
          aria-valuenow={Math.round(view.progressPct)}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label="Track progress"
        >
          <div className="progress-fill" style={{ width: `${view.progressPct}%` }} />
        </div>
        <span className="progress-time">{view.duration}</span>
      </div>

      <div className="transport">
        <button
          type="button"
          className={`btn${tunedIn ? ' is-active' : ''}`}
          onClick={onToggle}
          title={tunedIn ? 'Stop listening' : 'Tune in'}
          aria-label={tunedIn ? 'Stop listening' : 'Tune in'}
        >
          {tunedIn ? '❚❚' : '▶'}
        </button>
        <button
          type="button"
          className="btn"
          onClick={onRequest}
          title="Request a song"
          aria-label="Request a song"
        >
          ＋
        </button>
        <input
          className="volume"
          type="range"
          min={0}
          max={1}
          step={0.01}
          value={volume}
          onChange={(e) => onVolume(Number(e.target.value))}
          aria-label="Volume"
        />
        <div className={`transport-note${tune === 'error' ? ' is-error' : ''}`}>
          {tuneNote(tune, air)}
        </div>
      </div>
    </section>
  )
}
