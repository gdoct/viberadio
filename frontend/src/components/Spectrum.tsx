import { SPECTRUM_BAR_ATTR, SPECTRUM_GROUP_ATTR } from '../hooks/useSpectrum'

/** Bar count from the design; heights are driven by `useSpectrum`. */
const BARS = 56

const group = { [SPECTRUM_GROUP_ATTR]: true, 'aria-hidden': true } as const
const bar = { [SPECTRUM_BAR_ATTR]: true } as const

interface MeterProps {
  /** The station is playing something — drives the waveform. */
  onAir: boolean
  /** This listener has tuned in. Dimmed when not: lively bars over silent
      speakers read as broken audio rather than "press play". */
  listening: boolean
}

export function Spectrum({ onAir, listening }: MeterProps) {
  return (
    <div
      className={`spectrum${onAir && listening ? '' : ' is-idle'}`}
      {...group}
    >
      {Array.from({ length: BARS }, (_, i) => (
        <div key={i} className="spectrum-bar" {...bar} />
      ))}
    </div>
  )
}

/** The three-bar meter that sits in the DJ strip. */
const MINI_BARS = [
  { color: '#ff5630', height: 40 },
  { color: '#ff7a3c', height: 70 },
  { color: '#ffb020', height: 30 },
]

export function MiniMeter({ onAir, listening }: MeterProps) {
  return (
    <div
      className={`dj-meter${onAir && listening ? '' : ' is-idle'}`}
      {...group}
    >
      {MINI_BARS.map((b) => (
        <div
          key={b.color}
          className="dj-meter-bar"
          style={{ background: b.color, height: `${b.height}%` }}
          {...bar}
        />
      ))}
    </div>
  )
}
