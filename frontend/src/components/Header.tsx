import type { ChannelInfo, ChannelSummary } from '../api'
import { initial } from '../format'
import type { AirState } from '../station-view'
import { StationPicker } from './StationPicker'

const STATUS_LABEL: Record<AirState, string> = {
  air: 'ON AIR',
  mic: 'DJ ON MIC',
  off: 'OFF AIR',
  starting: 'SPINNING UP',
}

const STATUS_CLASS: Record<AirState, string> = {
  air: 'is-air',
  mic: 'is-mic',
  off: 'is-off',
  starting: 'is-starting',
}

interface Props {
  channel: ChannelInfo | null
  channels: ChannelSummary[]
  selected: string | null
  air: AirState
  onSelect: (slug: string) => void
}

export function Header({ channel, channels, selected, air, onSelect }: Props) {
  const current =
    channels.find((c) => c.slug === (selected ?? channel?.slug)) ?? null

  return (
    <header className="header">
      <div className="brand">
        <div className="brand-mark">
          {initial(current?.name ?? channel?.name, 'V')}
        </div>
        <StationPicker
          channels={channels}
          current={current}
          fallbackName={channel?.name ?? 'Vibe Radio'}
          onSelect={onSelect}
        />
        <div className="brand-meta">
          {current?.style ?? channel?.style ?? 'AI radio'} · AI Studio
        </div>
      </div>
      <div
        className={`status-pill ${STATUS_CLASS[air]}`}
        role="status"
        aria-live="polite"
      >
        <span className="status-dot" />
        <span className="status-text">{STATUS_LABEL[air]}</span>
      </div>
    </header>
  )
}
