import { useEffect, useRef, useState } from 'react'

import type { ChannelSummary, StationStatus } from '../api'

const STATUS_NOTE: Record<StationStatus, string> = {
  live: 'on air',
  starting: 'spinning up…',
  off: 'idle',
}

interface Props {
  channels: ChannelSummary[]
  current: ChannelSummary | null
  fallbackName: string
  onSelect: (slug: string) => void
}

export function StationPicker({
  channels,
  current,
  fallbackName,
  onSelect,
}: Props) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const pick = (slug: string) => {
    setOpen(false)
    onSelect(slug)
  }

  return (
    <div className="picker" ref={rootRef}>
      <button
        type="button"
        className="picker-trigger"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span className="brand-name">{current?.name ?? fallbackName}</span>
        <span className="picker-caret" aria-hidden="true">
          ▾
        </span>
      </button>

      {open && (
        <ul className="picker-menu" role="listbox" aria-label="Stations">
          {channels.map((channel) => {
            const selected = channel.slug === current?.slug
            return (
              <li key={channel.slug}>
                <button
                  type="button"
                  role="option"
                  aria-selected={selected}
                  className={`picker-option${selected ? ' is-selected' : ''}`}
                  onClick={() => pick(channel.slug)}
                >
                  <span className="picker-option-main">
                    <span className="picker-option-name">{channel.name}</span>
                    <span className="picker-option-style">{channel.style}</span>
                  </span>
                  <span className={`picker-status is-${channel.status}`}>
                    {STATUS_NOTE[channel.status]}
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
