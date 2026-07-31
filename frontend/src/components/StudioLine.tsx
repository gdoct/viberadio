import { useEffect, useLayoutEffect, useRef, useState, type FormEvent } from 'react'

import type { ThreadMessage, ThreadRole } from '../thread'

const ROW_CLASS: Record<ThreadRole, string> = {
  sys: 'is-center',
  dj: 'is-start',
  listener: 'is-end',
  pending: 'is-end',
}

const BUBBLE_CLASS: Record<ThreadRole, string> = {
  sys: 'is-sys',
  dj: 'is-dj',
  listener: 'is-listener',
  pending: 'is-pending',
}

interface Props {
  messages: ThreadMessage[]
  inputRef: React.RefObject<HTMLInputElement | null>
  onSend: (text: string) => Promise<void>
}

export function StudioLine({ messages, inputRef, onSend }: Props) {
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const logRef = useRef<HTMLDivElement | null>(null)
  const pinned = useRef(true)

  // Follow the log only while the listener is already at the bottom, so
  // scrolling back through the night's history isn't yanked away by a new line.
  useLayoutEffect(() => {
    const log = logRef.current
    if (log && pinned.current) log.scrollTop = log.scrollHeight
  }, [messages])

  useEffect(() => {
    const log = logRef.current
    if (!log) return
    const onScroll = () => {
      const distance = log.scrollHeight - log.scrollTop - log.clientHeight
      pinned.current = distance < 60
    }
    log.addEventListener('scroll', onScroll, { passive: true })
    return () => log.removeEventListener('scroll', onScroll)
  }, [])

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const text = draft.trim()
    if (!text || sending) return
    setSending(true)
    setError(null)
    try {
      await onSend(text)
      setDraft('')
      pinned.current = true
    } catch {
      setError('The studio line is down — message not sent.')
    } finally {
      setSending(false)
    }
  }

  return (
    <section className="panel studio">
      <div className="studio-head">
        <div className="studio-title">Studio line</div>
        <div className="studio-sub">
          requests are judged by the DJ and air after the current song
        </div>
      </div>

      <div className="chatlog" ref={logRef}>
        {messages.length === 0 ? (
          <div className="chatlog-empty">
            Nothing on the line yet.
            <br />
            Ask for a track and the DJ will weigh it up.
          </div>
        ) : (
          messages.map((msg) => (
            <div key={msg.id} className={`msg-row ${ROW_CLASS[msg.role]}`}>
              <div className={`bubble ${BUBBLE_CLASS[msg.role]}`}>
                {msg.role !== 'sys' && msg.who && (
                  <div className="bubble-who">{msg.who}</div>
                )}
                <div>{msg.text}</div>
                {msg.tag && <div className="bubble-tag">⏱ {msg.tag}</div>}
              </div>
            </div>
          ))
        )}
      </div>

      <form className="composer" onSubmit={submit}>
        {error && <div className="composer-hint is-error">{error}</div>}
        <div className="composer-box">
          <input
            ref={inputRef}
            className="composer-input"
            name="message"
            autoComplete="off"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Request a song…"
            maxLength={500}
            aria-label="Message the studio"
          />
          <button
            type="submit"
            className="composer-send"
            disabled={!draft.trim() || sending}
            aria-label="Send to the studio"
          >
            ➤
          </button>
        </div>
      </form>
    </section>
  )
}
