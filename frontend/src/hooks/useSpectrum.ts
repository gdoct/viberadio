import { useEffect, useRef } from 'react'

/** Marks a meter; each group is mapped across the spectrum independently. */
export const SPECTRUM_GROUP_ATTR = 'data-spectrum-group'
/** Marks a single bar within a group. */
export const SPECTRUM_BAR_ATTR = 'data-spectrum'

/**
 * Animates every meter on the page.
 *
 * When the listener is tuned in the heights come from the live FFT. When they
 * aren't, the station is still on air — we just can't hear it — so the bars run
 * a smooth synthetic waveform instead of sitting dead.
 *
 * Heights are written straight to the DOM rather than through state: this runs
 * at 60fps across ~60 bars, which React would re-render for no benefit.
 */
export function useSpectrum(analyser: AnalyserNode | null, onAir: boolean) {
  const onAirRef = useRef(onAir)
  onAirRef.current = onAir

  useEffect(() => {
    const smoothed: number[] = []
    const freq = analyser ? new Uint8Array(analyser.frequencyBinCount) : null
    let raf = 0

    const frame = (now: number) => {
      const t = now / 1000
      const live = onAirRef.current
      const useFFT = Boolean(analyser && freq && live)
      if (analyser && freq && useFFT) analyser.getByteFrequencyData(freq)

      // Each meter spans the whole spectrum on its own, so the DJ strip's
      // three bars read low/mid/high rather than the far end of the main meter.
      let slot = 0
      document
        .querySelectorAll<HTMLElement>(`[${SPECTRUM_GROUP_ATTR}]`)
        .forEach((group) => {
          const bars = group.querySelectorAll<HTMLElement>(
            `[${SPECTRUM_BAR_ATTR}]`,
          )
          bars.forEach((bar, i) => {
            let target: number
            if (useFFT && freq) {
              // Skip the top of the spectrum — it is mostly empty on 160k AAC
              // and would leave the right-hand bars flat.
              const idx = Math.floor((i / bars.length) * freq.length * 0.7)
              target = freq[idx] / 255
            } else if (live) {
              const phase = i * 0.7
              target =
                0.34 +
                0.3 * Math.sin(t * 3.1 + phase) +
                0.2 * Math.sin(t * 6.7 + phase * 1.7) +
                0.14 * Math.sin(t * 11.3 + phase * 0.6)
            } else {
              target = 0.05 + 0.03 * Math.sin(t * 1.2 + i * 0.5)
            }

            target = Math.max(0.04, Math.min(1, target))
            const prev = smoothed[slot]
            const next = prev == null ? target : prev + (target - prev) * 0.28
            smoothed[slot] = next
            slot += 1
            bar.style.height = `${(next * 100).toFixed(1)}%`
          })
        })

      raf = requestAnimationFrame(frame)
    }

    raf = requestAnimationFrame(frame)
    return () => cancelAnimationFrame(raf)
  }, [analyser])
}
