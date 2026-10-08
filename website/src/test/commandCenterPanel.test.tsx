import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { api } from '../api/client'
import * as transport from '../chat-core/transport/sendTurn'
import { createTestStore, renderWithProviders } from './helpers'
import CommandCenterPanel from '../pages/chat/command-center/CommandCenterPanel'

vi.mock('../pages/members/CrewDynamicDashboard', () => ({
  default: ({ target }: { target: { kind: string; slot?: string } }) =>
    <div data-testid="session-dynamic-dashboard" data-kind={target.kind} data-slot={target.slot}>dynamic dashboard</div>,
}))

function taskStore() {
  const initial = createTestStore().getState()
  return createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: true, slots: [
    { key: 'root', title: 'Conductor', messages: 0, running: true },
    { key: 'worker', title: 'Review worker', created_by: 'root', messages: 0, running: false },
    { key: 'adopted', title: 'Adopted tab', messages: 0, running: false, parent: { slot: 'root', key: 'dashboard:root' } },
  ] } })
}

describe('task dashboard host controls', () => {
  afterEach(() => vi.unstubAllGlobals())
  beforeEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
    // happy-dom has no layout; establish the panel width that selects tabs.
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(480)
    vi.spyOn(api, 'pendingQuestions').mockResolvedValue([])
    vi.spyOn(api, 'approvals').mockResolvedValue([])
    vi.spyOn(api, 'workflowRuns').mockResolvedValue({ runs: [] })
    vi.spyOn(api, 'sessionWorkProjection').mockResolvedValue({ value: { items: [
      { item_id: 'one', title: 'Accepted contract', state: 'accepted' },
      { item_id: 'two', title: 'Review changes', state: 'dispatched', status: 'blocked', summary: 'Needs evidence' },
    ] } })
  })

  it('renders a root session\'s own Dynamic Dashboard as the Overview, keyed by its slot', async () => {
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    const page = await screen.findByTestId('session-dynamic-dashboard')
    expect(page).toBeVisible()
    expect(page).toHaveAttribute('data-kind', 'session')
    expect(page).toHaveAttribute('data-slot', 'root')
    expect(screen.getByTestId('command-center-overview')).toContainElement(page)
    // The agent-published page and its request are gone with the HUD.
    expect(screen.queryByRole('button', { name: 'Create published view' })).not.toBeInTheDocument()
    expect(screen.queryByTestId('session-status-frame')).not.toBeInTheDocument()
    expect(screen.queryByTestId('task-dashboard-frame')).not.toBeInTheDocument()
  })

  it.each(['worker', 'adopted'])('gives a non-root (%s) session no dashboard and opens on Questions', async slot => {
    renderWithProviders(<CommandCenterPanel slot={slot} active />, { store: taskStore() })
    expect(await screen.findByRole('radio', { name: /Questions/ })).toBeChecked()
    expect(screen.queryByRole('radio', { name: /Overview/ })).not.toBeInTheDocument()
    expect(screen.queryByTestId('command-center-overview')).not.toBeInTheDocument()
    expect(screen.queryByTestId('session-dynamic-dashboard')).not.toBeInTheDocument()
  })

  it('does not mount the dashboard while the panel is not active', () => {
    renderWithProviders(<CommandCenterPanel slot="root" active={false} />, { store: taskStore() })
    expect(screen.queryByTestId('session-dynamic-dashboard')).not.toBeInTheDocument()
  })

  it('says a part is missing, not that fresh decisions are stale, when an optional source fails', async () => {
    vi.mocked(api.workflowRuns).mockRejectedValue(new Error('workflows not available'))
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    expect(await screen.findByText(/Some sources could not be loaded: workflow runs/)).toBeInTheDocument()
    expect(screen.queryByText(/The last known state may be out of date/)).not.toBeInTheDocument()
  })

  it('keeps the questions and approvals out of the Overview, each in its own tab', async () => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'worker', ask_id: 'ask', questions: [{ question: 'Which contract?', options: [{ label: 'Stable API' }] }] }])
    vi.mocked(api.approvals).mockResolvedValue([{ id: 'permission', slot: 'worker', tool: 'shell', tool_input: 'git status' }])
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    const page = await screen.findByTestId('session-dynamic-dashboard')
    const overview = screen.getByTestId('command-center-overview')
    expect(within(overview).queryByRole('button', { name: 'Approve once' })).not.toBeInTheDocument()
    expect(within(overview).queryByText('Which contract?')).not.toBeInTheDocument()
    expect(await screen.findByRole('radio', { name: 'Approvals 1' })).toBeVisible()
    fireEvent.click(screen.getByRole('radio', { name: 'Questions 1' }))
    expect(screen.getByText('Which contract?')).toBeVisible()
    expect(page).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: 'Approvals 1' }))
    expect(screen.getByRole('button', { name: 'Approve once' })).toBeVisible()
    fireEvent.click(screen.getByRole('radio', { name: /Overview/ }))
    expect(await screen.findByTestId('session-dynamic-dashboard')).toBeVisible()
  })

  it('names approval-only session state Needs input, not Questions', async () => {
    const initial = taskStore().getState()
    const store = createTestStore({ ...initial, dashboard: { ...initial.dashboard, slots: initial.dashboard.slots.map(s => ({ ...s, pending_approval: s.key === 'worker' })) } })
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store })
    expect(await screen.findByRole('radio', { name: 'Approvals 1' })).toBeVisible()
    expect(screen.getByRole('radio', { name: 'Questions' })).toBeVisible()
  })

  it('shows recorded session context on an approval without inventing a request reason', async () => {
    const initial = taskStore().getState()
    const store = createTestStore({ ...initial, dashboard: { ...initial.dashboard, slots: initial.dashboard.slots.map(slot => ({ ...slot, ...(slot.key === 'worker' ? { todo: { tasks: [], current: 'Validate release in isolated workspace' } } : {}) })) } })
    vi.mocked(api.approvals).mockResolvedValue([{ id: 'permission', slot: 'worker', tool: 'shell', tool_input: 'git status' }])
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store })
    fireEvent.click(await screen.findByRole('radio', { name: 'Approvals 1' }))
    expect(screen.getAllByText('Approvals')).toHaveLength(1)
    expect(screen.getByRole('radio', { name: /Approvals/ })).toHaveTextContent('1')
    const card = screen.getByRole('button', { name: 'Approve once' }).closest('section')!
    expect(within(card).getByText('Validate release in isolated workspace')).toBeVisible()
    expect(within(card).queryByText(/no production impact|continue automatically/i)).not.toBeInTheDocument()
  })

  it.each(['custom', 'option'])('retains a retired stateless %s draft across polls and section navigation', async kind => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'dashboard:worker', card_id: 'card-1', native: true, questions: [
      { question: 'Which contract?', options: [{ label: 'Stable API' }] },
    ] }])
    const { queryClient } = renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    fireEvent.click(await screen.findByRole('radio', { name: 'Questions 1' }))
    const input = screen.getByPlaceholderText(/type a custom answer/i)
    if (kind === 'custom') fireEvent.change(input, { target: { value: 'Keep my contract draft' } })
    else fireEvent.click(screen.getByText('Stable API'))
    fireEvent.click(screen.getByRole('radio', { name: /Approvals/ }))
    vi.mocked(api.pendingQuestions).mockResolvedValue([])
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['command-center', 'questions'] }) })
    fireEvent.click(screen.getByRole('radio', { name: /Questions/ }))
    expect(screen.getByRole('button', { name: 'Send answer' })).toBeEnabled()
    if (kind === 'custom') expect(input).toHaveValue('Keep my contract draft')
    // Clearing the actual draft abandons a retired card, rather than retaining it forever.
    if (kind === 'custom') fireEvent.change(input, { target: { value: '' } })
    else fireEvent.click(screen.getByText('Stable API'))
    await waitFor(() => expect(screen.queryByText('Which contract?')).not.toBeInTheDocument())
  })

  it.each(['failed', 'uncertain', 'accepted-dismiss-failed'])('handles a retired draft send without losing or duplicating it (%s)', async outcome => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'worker', card_id: 'card', questions: [{ question: 'Which contract?', options: [{ label: 'Stable API' }] }] }])
    const send = vi.spyOn(transport, 'sendTurn')
    if (outcome === 'failed') send.mockRejectedValue(new Error('Offline'))
    else if (outcome === 'uncertain') send.mockResolvedValue({ status: 'unknown', body: {} })
    else send.mockResolvedValue({ status: 'dispatched', body: {} })
    const dismiss = vi.spyOn(api, 'dismissQuestionCard').mockRejectedValue(new Error('Retirement failed'))
    const { queryClient } = renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    fireEvent.click(await screen.findByRole('radio', { name: 'Questions 1' }))
    const input = screen.getByPlaceholderText(/type a custom answer/i)
    fireEvent.change(input, { target: { value: 'Drafted response' } })
    vi.mocked(api.pendingQuestions).mockResolvedValue([])
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['command-center', 'questions'] }) })
    fireEvent.click(screen.getByRole('button', { name: 'Send answer' }))
    await waitFor(() => expect(send).toHaveBeenCalledTimes(1))
    if (outcome === 'accepted-dismiss-failed') {
      await waitFor(() => expect(screen.queryByText('Which contract?')).not.toBeInTheDocument())
      expect(dismiss).toHaveBeenCalledWith('worker', 'card')
      expect(screen.queryByRole('button', { name: 'Send answer' })).not.toBeInTheDocument()
    } else {
      await screen.findByRole('alert')
      expect(input).toHaveValue('Drafted response')
      expect(screen.getByRole('button', { name: 'Send answer' })).toBeEnabled()
      expect(dismiss).not.toHaveBeenCalled()
    }
  })

  it('keeps every section accessible with compact labels in a 320px panel', async () => {
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(320)
    vi.stubGlobal('ResizeObserver', class {
      constructor(private callback: ResizeObserverCallback) {}
      observe(target: Element) { this.callback([{ target, contentRect: { width: 320 } } as ResizeObserverEntry], this as unknown as ResizeObserver) }
      unobserve() {}
      disconnect() {}
    })
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    const approvals = await screen.findByRole('radio', { name: /Approvals/ })
    expect(approvals).toHaveTextContent('Approvals')
    fireEvent.click(approvals)
    expect(approvals).toHaveTextContent('Approvals')
    expect(screen.getByRole('radio', { name: /Overview/ })).toHaveTextContent('Overview')
    expect(screen.getByRole('radio', { name: /Questions/ })).toHaveTextContent('Questions')
  })

  it('keeps a worker answer draft while switching between questions and approvals', async () => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'worker', ask_id: 'ask', questions: [
      { question: 'Which contract?', options: [{ label: 'Stable API' }] },
    ] }])
    vi.mocked(api.approvals).mockResolvedValue([{ id: 'permission', slot: 'dashboard:worker', tool: 'shell', tool_input: 'git status' }])
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    fireEvent.click(await screen.findByRole('radio', { name: 'Questions 1' }))
    fireEvent.click(await screen.findByText('Stable API'))
    fireEvent.click(screen.getByRole('radio', { name: /Approvals/ }))
    expect(screen.getByRole('button', { name: 'Approve once' })).toBeVisible()
    expect(screen.getByText(/Approval required/)).toHaveTextContent('Normal')
    fireEvent.click(screen.getByRole('radio', { name: /Questions/ }))
    expect(screen.getByRole('button', { name: 'Send answer' })).toBeEnabled()
    fireEvent.click(screen.getByRole('radio', { name: /Overview/ }))
    expect(await screen.findByTestId('session-dynamic-dashboard')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Send answer', hidden: true })).not.toBeVisible()
  })
})
