// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { focusManager, useQueryClient } from '@tanstack/react-query'
import { createTestStore, renderHookWithProviders } from './helpers'
import { api } from '../api/client'
import { missingSourcesNotice, useCommandCenter } from '../pages/chat/command-center/useCommandCenter'
import { fmtList } from '../i18n/format'
import { i18nT } from '../i18n/t'
import { teamRoots } from '../pages/chat/command-center/model'

function store() {
  const initial = createTestStore().getState()
  return createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: true, slots: [
    { key: 'root', title: 'Conductor', messages: 0, running: true },
    { key: 'builder', created_by: 'root', messages: 0, running: false },
    { key: 'unrelated', messages: 0, running: true },
  ] } })
}

describe('task dashboard sources and containment', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.spyOn(api, 'pendingQuestions').mockResolvedValue([])
    vi.spyOn(api, 'approvals').mockResolvedValue([])
    vi.spyOn(api, 'workflowRuns').mockResolvedValue({ runs: [] })
    vi.spyOn(api, 'sessionWorkProjection').mockResolvedValue({ value: { items: [] } })
  })

  it('never falls back to the whole fleet while the owning slot is unresolved', () => {
    const { result } = renderHookWithProviders(() => useCommandCenter(null), { store: store() })
    expect(result.current.nodes).toEqual([])
    expect(result.current.attention).toEqual([])
    expect(api.pendingQuestions).not.toHaveBeenCalled()
  })

  it('reads the fleet only when explicitly requested, without per-session work queries', async () => {
    const { result } = renderHookWithProviders(() => useCommandCenter(null, true, 'fleet'), { store: store() })
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.nodes.map(n => n.slot)).toEqual(['root', 'builder', 'unrelated'])
    expect(api.sessionWorkProjection).not.toHaveBeenCalled()
    expect(result.current.stale).toBe(false)
    expect(result.current.updatedAt).toBeGreaterThan(0)
  })

  it('reports unavailable sources instead of claiming no questions are pending', async () => {
    vi.mocked(api.pendingQuestions).mockRejectedValue(new Error('offline'))
    const { result } = renderHookWithProviders(() => useCommandCenter('root'), { store: store() })
    await waitFor(() => expect(result.current.stale).toBe(true))
    expect(result.current.updatedAt).toBe(0)
  })

  it('polls no source in any scope, leaving refresh to the frames that announce changes', async () => {
    const intervals = (client: ReturnType<typeof useQueryClient>) => client.getQueryCache().findAll({ queryKey: ['command-center'] })
      .flatMap(q => q.observers.map(o => o.options.refetchInterval))
    for (const [root, scope] of [['root', 'task'], [null, 'fleet']] as const) {
      const view = renderHookWithProviders(() => ({ ...useCommandCenter(root, true, scope), queryClient: useQueryClient() }), { store: store() })
      await waitFor(() => expect(view.result.current.loading).toBe(false))
      const seen = intervals(view.result.current.queryClient)
      expect(seen.length).toBeGreaterThanOrEqual(2)
      expect(seen.every(i => !i)).toBe(true)
      view.unmount()
    }
    expect(api.pendingQuestions).toHaveBeenCalledTimes(2)
  })

  it('finds a slot\'s team roots by walking its creators, stopping on a cycle', () => {
    const slots = [
      { key: 'root', messages: 0, running: false }, { key: 'mid', messages: 0, running: false, created_by: 'dashboard:root' },
      { key: 'leaf', messages: 0, running: false, created_by: 'mid' },
      { key: 'a', messages: 0, running: false, created_by: 'b' }, { key: 'b', messages: 0, running: false, created_by: 'a' },
    ]
    expect(teamRoots(slots, 'dashboard:leaf')).toEqual(['leaf', 'mid', 'root'])
    expect(teamRoots(slots, 'root')).toEqual(['root'])
    expect(teamRoots(slots, 'unknown')).toEqual(['unknown'])
    expect(teamRoots(slots, 'a')).toEqual(['a', 'b'])
  })

  it('reads the work board for the task panel and never refetches a healthy source on focus', async () => {
    const initial = store().getState()
    const solo = createTestStore({ ...initial, dashboard: { ...initial.dashboard, slots: [{ key: 'root', messages: 0, running: true }] } })
    const panel = renderHookWithProviders(() => ({ ...useCommandCenter('root'), queryClient: useQueryClient() }), { store: solo })
    await waitFor(() => expect(panel.result.current.loading).toBe(false))
    expect(api.sessionWorkProjection).toHaveBeenCalledWith('root')
    const own = (key: readonly unknown[]) => key[0] === 'command-center' || key[0] === 'global-approvals'
    const observers = panel.result.current.queryClient.getQueryCache().getAll().filter(q => own(q.queryKey)).flatMap(q => q.observers)
    expect(observers.length).toBeGreaterThan(0)
    // A healthy source is left to its frames; only a failed one re-reads on focus.
    const onFocus = (o: (typeof observers)[number]) => {
      const option = o.options.refetchOnWindowFocus
      return typeof option === 'function' ? option(o.getCurrentQuery()) : option
    }
    expect(observers.every(o => onFocus(o) === false)).toBe(true)
  })

  it('keeps decisions fresh when an optional source fails, and shares the app approvals cache', async () => {
    vi.mocked(api.workflowRuns).mockRejectedValue(new Error('workflows not available'))
    const { result } = renderHookWithProviders(() => ({ ...useCommandCenter('root'), queryClient: useQueryClient() }), { store: store() })
    await waitFor(() => expect(result.current.loading).toBe(false))
    await waitFor(() => expect(result.current.queryClient.getQueryState(['command-center', 'workflows'])?.status).toBe('error'))
    expect(result.current.stale).toBe(false)
    // The missing source is still reported, not passed off as "no runs".
    expect(result.current.missing).toEqual(['runs'])
    expect(result.current.updatedAt).toBeGreaterThan(0)
    expect(result.current.queryClient.getQueryState(['global-approvals'])?.status).toBe('success')
    // No workflow frame may ever come, so the failed source re-reads on focus.
    vi.mocked(api.workflowRuns).mockResolvedValue({ runs: [] })
    vi.mocked(api.pendingQuestions).mockClear()
    act(() => { focusManager.setFocused(false); focusManager.setFocused(true) })
    await waitFor(() => expect(result.current.missing).toEqual([]))
    expect(api.pendingQuestions).not.toHaveBeenCalled()
    vi.mocked(api.pendingQuestions).mockRejectedValue(new Error('offline'))
    await act(async () => { await result.current.queryClient.refetchQueries({ queryKey: ['command-center', 'questions'] }) })
    await waitFor(() => expect(result.current.stale).toBe(true))
    expect(result.current.missing).toEqual([])
  })

  it('names no missing source until the decision reads have answered', async () => {
    vi.mocked(api.workflowRuns).mockRejectedValue(new Error('workflows not available'))
    vi.mocked(api.pendingQuestions).mockReturnValue(new Promise(() => {}))
    const { result } = renderHookWithProviders(() => ({ ...useCommandCenter('root'), queryClient: useQueryClient() }), { store: store() })
    await waitFor(() => expect(result.current.queryClient.getQueryState(['command-center', 'workflows'])?.status).toBe('error'))
    expect(result.current.missing).toEqual([])
  })

  it('names every failed optional source in one notice, saying the reassurance once', () => {
    expect(missingSourcesNotice([])).toBeNull()
    const both = missingSourcesNotice(['runs', 'work'])!
    expect(both).toBe(i18nT('commandCenter.partial_sources', { sources: fmtList([i18nT('commandCenter.source_runs'), i18nT('commandCenter.source_work')]) }))
    expect(both).toContain(i18nT('commandCenter.source_runs'))
    expect(both).toContain(i18nT('commandCenter.source_work'))
  })

  it('retains only stateless drafts by exact normalized slot and card, clearing on scope changes', async () => {
    let root: string | null = 'root'
    let scope: 'task' | 'fleet' = 'task'
    const { result, rerender } = renderHookWithProviders(() => useCommandCenter(root, true, scope), { store: store() })
    await waitFor(() => expect(result.current.loading).toBe(false))
    const questions = [{ question: 'Which scope?', options: [{ label: 'Stable' }] }]
    const own = { slot: 'dashboard:root', card_id: 'same', questions }
    act(() => {
      result.current.onQuestionDraftChange(own, true)
      result.current.onQuestionDraftChange({ slot: 'builder', card_id: 'same', questions }, true)
      result.current.onQuestionDraftChange({ slot: 'root', card_id: 'other', questions }, true)
      result.current.onQuestionDraftChange({ slot: 'root', ask_id: 'blocked', card_id: 'blocked-card', questions }, true)
      result.current.onQuestionDraftChange({ slot: 'root', questions }, true)
    })
    expect(result.current.attention.map(a => a.id)).toEqual(['question:root:same', 'question:builder:same', 'question:root:other'])
    const departingCallback = result.current.onQuestionDraftChange
    act(() => result.current.onQuestionDraftChange({ ...own, slot: 'root' }, false))
    expect(result.current.attention.map(a => a.id)).toEqual(['question:builder:same', 'question:root:other'])
    root = 'unrelated'
    rerender()
    expect(result.current.attention).toEqual([])
    root = 'root'
    rerender()
    expect(result.current.attention).toEqual([])
    root = null
    scope = 'fleet'
    rerender()
    act(() => result.current.onQuestionDraftChange(own, true))
    act(() => departingCallback(own, false))
    expect(result.current.attention.map(a => a.id)).toEqual(['question:root:same'])
    scope = 'task'
    rerender()
    expect(result.current.attention).toEqual([])
  })

  it('reports a draft for a BLOCKING ask too, while still refusing to retain its card', async () => {
    // `hasQuestionDraft` is what a host keeps its panel mounted on, so it has to
    // be true of every question being typed into. Retention is the narrower rule:
    // a blocking `ask_id` card is owned by the live list and must not be resurrected
    // past its retirement. Read off the retention map, a blocking ask reported no
    // draft at all -- the host released the panel and the typed answer went with
    // the unmount.
    const { result, rerender } = renderHookWithProviders(() => useCommandCenter('root'), { store: store() })
    await waitFor(() => expect(result.current.loading).toBe(false))
    const questions = [{ question: 'Which scope?', options: [{ label: 'Stable' }] }]
    const blocking = { slot: 'root', ask_id: 'blocked', card_id: 'blocked-card', questions }
    expect(result.current.hasQuestionDraft).toBe(false)
    act(() => { result.current.onQuestionDraftChange(blocking, true) })
    expect(result.current.hasQuestionDraft).toBe(true)
    // Still not retained: the attention list carries nothing this hook invented.
    expect(result.current.attention).toEqual([])
    act(() => { result.current.onQuestionDraftChange(blocking, false) })
    expect(result.current.hasQuestionDraft).toBe(false)
    // A stateless card reports the same way, and is retained as before.
    const stateless = { slot: 'root', card_id: 'same', questions }
    act(() => { result.current.onQuestionDraftChange(stateless, true) })
    expect(result.current.hasQuestionDraft).toBe(true)
    expect(result.current.attention.map(a => a.id)).toEqual(['question:root:same'])
    // A question with neither id cannot be tracked, and must not claim a draft.
    act(() => { result.current.onQuestionDraftChange(stateless, false) })
    act(() => { result.current.onQuestionDraftChange({ slot: 'root', questions }, true) })
    expect(result.current.hasQuestionDraft).toBe(false)
    rerender()
    expect(result.current.hasQuestionDraft).toBe(false)
    // A trailing [OPTIONS:] ask carries no id either, but a pick in it is a draft
    // the host must hold the panel for; it is never retained as a card.
    const followUp = { slot: 'root', followUp: true, questions }
    act(() => { result.current.onQuestionDraftChange(followUp, true) })
    expect(result.current.hasQuestionDraft).toBe(true)
    expect(result.current.attention).toEqual([])
    act(() => { result.current.onQuestionDraftChange(followUp, false) })
    expect(result.current.hasQuestionDraft).toBe(false)
  })

})
