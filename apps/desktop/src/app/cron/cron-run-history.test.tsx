import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { type CronJobHistory, getCronJobHistory } from '@/api/cron'
import { en } from '@/i18n/en'
import { $connection } from '@/store/session'

import { CronRunHistory, historyTime } from './cron-run-history'

vi.mock('@/api/cron', () => ({ getCronJobHistory: vi.fn() }))
const load = vi.mocked(getCronJobHistory)

const receipt = (id = 'execution-only'): CronJobHistory => ({
  runs: [],
  execution_history: {
    contract: 'hermes.execution-history/v1',
    profile: 'worker',
    records: [
      {
        id,
        sequence: 8,
        job_id: 'script-job',
        status: 'completed',
        started_at: '2026-09-09T04:00:00-03:00',
        claimed_at: null,
        finished_at: null,
        delivery_outcome: 'failed',
        functional_result: { outcome: 'deferred', exit_code: 0 },
        message:
          'Aguardando condição de execução\nDependência: extract\nEntrega da mensagem falhou; o trabalho não será repetido.'
      }
    ]
  }
})

beforeEach(() => {
  load.mockReset()
  vi.spyOn(globalThis.document, 'visibilityState', 'get').mockReturnValue('visible')
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('native cron history in both Desktop surfaces', () => {
  it.each([false, true])(
    'shows script work and independent delivery without a fake conversation (compact=%s)',
    async compact => {
      load.mockResolvedValue(receipt())
      const navigate = vi.fn()
      render(
        <CronRunHistory c={en.cron} compact={compact} jobId="script-job" onOpenSession={navigate} profile="worker" />
      )
      expect(await screen.findByText('execution-only')).toBeTruthy()
      expect(load).toHaveBeenCalledWith('script-job', compact ? 5 : 20, 'worker')
      expect(screen.queryByText(en.cron.noRuns)).toBeNull()
      expect(screen.getByText(/Process: completed · Message delivery: failed/)).toBeTruthy()
      expect(screen.getByText(/2026-09-09T07:00:00.000Z · Aguardando/)).toBeTruthy()
      expect(screen.queryByRole('button')).toBeNull()
      expect(navigate).not.toHaveBeenCalled()
    }
  )

  it('keeps older-backend conversations navigable without asserting job completion', async () => {
    load.mockResolvedValue({
      runs: [{ id: 'cron_script-job_old', started_at: 10, title: 'Old conversation' } as never]
    })
    const navigate = vi.fn()
    render(<CronRunHistory c={en.cron} jobId="script-job" onOpenSession={navigate} />)
    expect(await screen.findByText(en.cron.historyLegacy)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Old conversation/ }))
    expect(navigate).toHaveBeenCalledWith('cron_script-job_old')
    expect(screen.queryByText(en.cron.noRuns)).toBeNull()
  })

  it('reports read failure and retries the read without firing work', async () => {
    load.mockRejectedValueOnce(new Error('unavailable')).mockResolvedValueOnce(receipt())
    render(<CronRunHistory c={en.cron} jobId="script-job" />)
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.queryByText(en.cron.noRuns)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: en.cron.historyRetry }))
    expect(await screen.findByText('execution-only')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(load).toHaveBeenCalledTimes(2)
  })

  it('ignores a slow older response after a newer refresh completes', async () => {
    let finishOld!: (history: CronJobHistory) => void
    load
      .mockReturnValueOnce(
        new Promise(resolve => {
          finishOld = resolve
        })
      )
      .mockResolvedValueOnce(receipt('newer'))
    render(<CronRunHistory c={en.cron} jobId="script-job" />)
    fireEvent(globalThis.document, new Event('visibilitychange'))
    expect(await screen.findByText('newer')).toBeTruthy()
    await act(async () => {
      finishOld(receipt('older'))
    })
    expect(screen.queryByText('older')).toBeNull()
    expect(screen.getByText('newer')).toBeTruthy()
  })

  it('does not show the previous profile while the new one is loading', async () => {
    load.mockResolvedValueOnce(receipt('profile-one')).mockReturnValueOnce(new Promise(() => {}))
    const view = render(<CronRunHistory c={en.cron} jobId="script-job" profile="one" />)
    expect(await screen.findByText('profile-one')).toBeTruthy()
    view.rerender(<CronRunHistory c={en.cron} jobId="script-job" profile="two" />)
    await waitFor(() => expect(load).toHaveBeenLastCalledWith('script-job', 20, 'two'))
    expect(screen.queryByText('profile-one')).toBeNull()
  })

  it('does not load history for a hidden sidebar pane', () => {
    render(<CronRunHistory c={en.cron} compact jobId="script-job" visible={false} />)
    expect(load).not.toHaveBeenCalled()
  })

  it('invalidates cached history when switching hosts with the same profile and job id', async () => {
    const previous = $connection.get()
    load.mockResolvedValueOnce(receipt('first-host')).mockResolvedValueOnce(receipt('second-host'))

    try {
      render(<CronRunHistory c={en.cron} jobId="script-job" profile="default" />)
      expect(await screen.findByText('first-host')).toBeTruthy()
      act(() => $connection.set({ connectionId: 'second-host', profile: 'default' } as never))
      expect(await screen.findByText('second-host')).toBeTruthy()
      expect(screen.queryByText('first-host')).toBeNull()
    } finally {
      cleanup()
      $connection.set(previous)
    }
  })

  it.each([null, undefined, '', 'invalid', '2026-09-09T12:00:00', Number.NaN])(
    'does not invent a timestamp for %s',
    value => {
      expect(historyTime(value)).toBe('—')
    }
  )
})
