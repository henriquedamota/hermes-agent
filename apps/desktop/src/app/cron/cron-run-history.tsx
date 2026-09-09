import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import { type CronJobHistory, getCronJobHistory } from '@/api/cron'
import { Button } from '@/components/ui/button'
import { ErrorBanner } from '@/components/ui/error-state'
import { GlyphSpinner } from '@/components/ui/glyph-spinner'
import type { Translations } from '@/i18n'
import { $changeEventsAvailable, $cronChangeTick } from '@/store/live-sync'
import { $connection, $selectedStoredSessionId } from '@/store/session'

import { PanelSectionLabel } from '../overlays/panel'

interface CronRunHistoryProps {
  c: Translations['cron']
  jobId: string
  profile?: string
  compact?: boolean
  visible?: boolean
  onOpenSession?: (sessionId: string) => void
}

export function historyTime(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') {
    return '—'
  }

  if (typeof value === 'string' && !/(Z|[+-]\d{2}:\d{2})$/i.test(value)) {
    return '—'
  }

  const date = new Date(typeof value === 'number' ? value * 1000 : value)

  // ISO includes the zone and cannot silently render an invalid timestamp as now.
  return Number.isNaN(date.valueOf()) ? '—' : date.toISOString()
}

// One reader and presentation path for the detail pane and sidebar peek. Runs
// without a conversation remain inspectable; only real session ids navigate.
export function CronRunHistory({
  c,
  jobId,
  profile,
  compact = false,
  visible = true,
  onOpenSession
}: CronRunHistoryProps) {
  const connection = useStore($connection)
  const selectedSessionId = useStore($selectedStoredSessionId)
  const key = JSON.stringify([connection?.connectionId, connection?.profile, profile, jobId])
  const [loaded, setLoaded] = useState<{ key: string; history: CronJobHistory } | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const [refresh, setRefresh] = useState(0)
  const changeEventsAvailable = useStore($changeEventsAvailable)
  const cronChangeTick = useStore($cronChangeTick)
  const history = loaded?.key === key ? loaded.history : null
  const failed = failure === key

  useEffect(() => {
    let cancelled = false
    let generation = 0

    if (!visible) {
      return
    }

    const load = async () => {
      const current = ++generation

      try {
        const result = await getCronJobHistory(jobId, compact ? 5 : 20, profile)

        if (!cancelled && current === generation) {
          setLoaded({ key, history: result })
          setFailure(null)
        }
      } catch {
        if (!cancelled && current === generation) {
          setFailure(key)
        }
      }
    }

    void load()

    const onVisible = () => {
      if (document.visibilityState === 'visible') {
        void load()
      }
    }

    const interval = window.setInterval(onVisible, changeEventsAvailable ? 60_000 : 8000)
    document.addEventListener('visibilitychange', onVisible)

    return () => {
      cancelled = true
      window.clearInterval(interval)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [changeEventsAvailable, compact, cronChangeTick, jobId, key, profile, refresh, visible])

  if (!visible) {
    return null
  }

  const records = history?.execution_history?.records ?? []

  return (
    <div className="space-y-2 text-xs">
      {!compact && <PanelSectionLabel>{c.runHistory}</PanelSectionLabel>}
      {failed && (
        <div role="alert">
          <ErrorBanner>{c.historyUnavailable}</ErrorBanner>
          <Button onClick={() => setRefresh(value => value + 1)} size="inline" variant="text">
            {c.historyRetry}
          </Button>
        </div>
      )}
      {!history && !failed && <GlyphSpinner ariaLabel={c.loading} />}
      {history && (
        <>
          {!history.execution_history && <p className="text-muted-foreground">{c.historyLegacy}</p>}
          {history.execution_history && records.length === 0 && !failed && (
            <p className="text-muted-foreground">{c.noRuns}</p>
          )}
          {records.map(run => (
            <details key={run.id}>
              <summary className="break-words text-foreground/85">
                {historyTime(run.started_at ?? run.claimed_at)} · {run.message.split('\n')[0]}
              </summary>
              <p className="whitespace-pre-wrap break-words">{run.message}</p>
              <p className="break-words text-muted-foreground">
                {c.historyProcess}: {run.status} · {c.historyDelivery}: {run.delivery_outcome ?? '—'}
              </p>
              <p className="break-all text-muted-foreground">{run.id}</p>
            </details>
          ))}
          {history.runs.length > 0 && (
            <>
              <PanelSectionLabel>{c.historyConversations}</PanelSectionLabel>
              {history.runs.map(run => (
                <div key={run.id}>
                <Button
                  aria-current={run.id === selectedSessionId ? 'page' : undefined}
                    disabled={!onOpenSession}
                    onClick={() => onOpenSession?.(run.id)}
                    size="inline"
                    variant="text"
                  >
                    {historyTime(run.started_at)} · {run.title?.trim() || run.preview?.trim() || run.id}
                  </Button>
                </div>
              ))}
            </>
          )}
        </>
      )}
    </div>
  )
}
