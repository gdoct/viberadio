/** Small display helpers shared across the studio panels. */

/** Seconds → `m:ss`, clamped at zero. Used for the transport read-outs. */
export function clock(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return '0:00'
  const total = Math.max(0, Math.floor(seconds))
  const mins = Math.floor(total / 60)
  const secs = total % 60
  return `${mins}:${secs.toString().padStart(2, '0')}`
}

/** "Artist — Title", falling back to the title alone for untagged tracks. */
export function trackLabel(
  track: { title: string; artist: string | null } | null,
): string {
  if (!track) return ''
  return track.artist ? `${track.artist} — ${track.title}` : track.title
}

/** First grapheme of a name, for the logo tile and DJ avatar. */
export function initial(name: string | undefined, fallback = '·'): string {
  const trimmed = (name ?? '').trim()
  return trimmed ? trimmed[0].toUpperCase() : fallback
}
