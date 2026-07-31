/**
 * The station streams a single AAC rendition with no alternate audio,
 * subtitles or DRM, so the light hls.js build is enough — and roughly half the
 * size. It ships no typings of its own, and its API is a strict subset of the
 * full build's, so the full build's types describe it accurately.
 */
declare module 'hls.js/light' {
  export * from 'hls.js'
  export { default } from 'hls.js'
}
