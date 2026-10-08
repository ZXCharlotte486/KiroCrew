import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { __resetPanelTabs } from '../../hooks/usePanelTabs'
import { memberProjectionStore } from '../../state/memberProjectionStore'

/* "New conversation" on a crewmate's DM. The model forgets; nothing is deleted.
 *
 * Three things have to hold for that to be honest, and each one fails quietly:
 *
 * `replay: false` must be SENT. The route defaults it to true, which replays the
 * discarded history straight back into the fresh context — so a call that omits
 * the flag answers 200 while changing nothing the user can see.
 *
 * The ask must come first, and it must say what survives. The context is gone
 * for good once the call lands.
 *
 * A refusal must be SHOWN. The route answers 409 for busy state the page cannot
 * see (an inbound channel message, a turn admitted between the render and the
 * press), and a swallowed 409 reads as "nothing happened" over a conversation
 * the model still remembers. */

vi.mock('../../api/client', () => ({
  api: {
    members: vi.fn(),
    teams: { list: vi.fn(() => Promise.resolve({ teams: [] })) },
    memberThread: vi.fn(),
    memberActivity: vi.fn(() => Promise.resolve({ slug: '', member: '', capped: false, entries: [] })),
    memberBriefing: vi.fn(() => Promise.resolve({ slug: '', member: '', supported: true, text: '', updated_ts: null, redacted: false, truncated: false })),
    sessionCrewLogProjections: vi.fn(() => Promise.resolve({ folds: {}, resolved: true, writesDrained: true })),
    memberPanel: vi.fn(() => Promise.resolve({ panel: null, html: null })),
    autonudgeList: vi.fn(() => Promise.resolve({ enabled: true, loops: [] })),
    chatSlotResetConversation: vi.fn(),
  },
}))

vi.mock('../chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../chat/FilesHomePanel', () => ({ default: () => null }))
vi.mock('../chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../../components/WebPreviewPanel', () => ({ default: () => null }))
vi.mock('../../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../../components/CliPanel', () => ({
  default: () => null,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => true,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../../hooks/useDevMode', () => ({ useDevMode: () => false }))

/* The pane as a stub that echoes the boundary it was handed: whether the page
 * reads the projection is the page's half of the feature, and the drawing of it
 * is pinned in ChatPane.conversationBoundary.test.tsx. */
vi.mock('../../components/ChatPane', () => ({
  default: ({ slotKey, conversationStartTs }: { slotKey: string; conversationStartTs?: number }) => (
    <div data-testid="chat-pane-stub" data-boundary={conversationStartTs ?? ''}>{slotKey}</div>
  ),
}))

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>()
  return { ...actual, useNavigate: () => vi.fn() }
})

import { api } from '../../api/client'
import MembersPage from './MembersPage'

function row(overrides: Record<string, unknown> = {}) {
  return {
    name: 'oncall', slug: 'oncall', bound: false, slot_key: '', running: false,
    kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '',
    ...overrides,
  }
}

const reset = () => api.chatSlotResetConversation as ReturnType<typeof vi.fn>

async function openThread(overrides: Record<string, unknown> = {}) {
  ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: [row(overrides)], default_agent: 'kirocrew' })
  ;(api.memberThread as ReturnType<typeof vi.fn>).mockResolvedValue({ slot_key: 'member-oncall', slug: 'oncall', member: 'oncall', created: true })
  renderWithProviders(<MembersPage />)
  const rowButton = await screen.findByText('oncall')
  act(() => { fireEvent.click(rowButton) })
  await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall'))
}

/** Press the header control and answer its dialog. */
async function pressAndConfirm() {
  const button = await screen.findByTestId('member-new-conversation')
  act(() => { fireEvent.click(button) })
  const confirm = await screen.findByRole('button', { name: 'Start a new conversation' })
  await act(async () => { fireEvent.click(confirm) })
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  // The projection store is process-wide and seeded from each roster read, so a
  // boundary one case planted would otherwise still be there for the next.
  memberProjectionStore.clear()
  __resetPanelTabs()
  Object.defineProperty(window, 'innerWidth', { value: 1440, configurable: true, writable: true })
  reset().mockResolvedValue({ slot: 'member-oncall', reset: true, replay: false })
})

describe('the New conversation control', () => {
  it('asks before it acts, and says what survives', async () => {
    await openThread()

    const button = await screen.findByTestId('member-new-conversation')
    act(() => { fireEvent.click(button) })

    expect(reset()).not.toHaveBeenCalled()
    const body = await screen.findByText(/forgets this conversation/)
    // Named, not templated: the copy carries `{{name}}`, and a body handed no
    // interpolation renders that placeholder to the user verbatim.
    expect(body.textContent).toMatch(/^oncall forgets this conversation/)
    expect(body.textContent).toMatch(/long-term memory stays/)
    expect(body.textContent).toMatch(/Show earlier messages/)
  })

  it('does nothing when the ask is declined', async () => {
    await openThread()

    const button = await screen.findByTestId('member-new-conversation')
    act(() => { fireEvent.click(button) })
    const cancel = await screen.findByRole('button', { name: 'Cancel' })
    await act(async () => { fireEvent.click(cancel) })

    expect(reset()).not.toHaveBeenCalled()
  })

  it('posts the reset for the open slot with replay explicitly off', async () => {
    await openThread()

    await pressAndConfirm()

    await waitFor(() => expect(reset()).toHaveBeenCalledWith('member-oncall'))
  })

  it('is disabled while the crewmate is working', async () => {
    await openThread({ running: true })

    expect(await screen.findByTestId('member-new-conversation')).toBeDisabled()
  })

  it('shows a refusal instead of swallowing it', async () => {
    await openThread()
    reset().mockRejectedValueOnce(new Error('a turn is in flight'))

    await pressAndConfirm()

    const notice = await screen.findByTestId('member-new-conversation-error')
    expect(notice.textContent).toMatch(/a turn is in flight/)
  })

  it('hands the pane the boundary the projection recorded', async () => {
    await openThread({
      projections: {
        asOfSeq: 4,
        values: {
          roster: {
            name: 'oncall',
            slug: 'oncall',
            conversation_starts: { 'member-oncall': { message_seq: 12, ts: 1_700_000_000_000 } },
          },
        },
      },
    })

    await waitFor(() =>
      expect(screen.getByTestId('chat-pane-stub').getAttribute('data-boundary')).toBe('1700000000000'),
    )
  })

  it('hands the pane no boundary for a slot that was never reset', async () => {
    await openThread({
      projections: {
        asOfSeq: 4,
        values: {
          roster: {
            name: 'oncall',
            slug: 'oncall',
            conversation_starts: { 'member-someone-else': { message_seq: 3, ts: 1_700_000_000_000 } },
          },
        },
      },
    })

    expect(screen.getByTestId('chat-pane-stub').getAttribute('data-boundary')).toBe('')
  })
})
