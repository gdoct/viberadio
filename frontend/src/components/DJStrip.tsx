import { initial } from '../format'
import type { AirState } from '../station-view'
import { MiniMeter } from './Spectrum'

interface Props {
  name: string
  badge: string
  badgeClass: string
  status: string
  air: AirState
  listening: boolean
}

export function DJStrip({
  name,
  badge,
  badgeClass,
  status,
  air,
  listening,
}: Props) {
  const onAir = air === 'air' || air === 'mic'
  return (
    <section className="panel dj-strip">
      <div className={`dj-avatar${onAir ? ' is-live' : ''}`} aria-hidden="true">
        {initial(name, 'D')}
      </div>
      <div className="dj-body">
        <div className="dj-head">
          <span className="dj-name">{name}</span>
          <span className={`dj-badge ${badgeClass}`}>{badge}</span>
        </div>
        <div className="dj-status">{status}</div>
      </div>
      <MiniMeter onAir={onAir} listening={listening} />
    </section>
  )
}
