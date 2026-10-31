/**
 * App message actions sit in a message's More menu, by role, and a click hands
 * the layer that one message and nothing else.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import AssistantMessage from '../pages/chat/AssistantMessage'
import UserMessage from '../pages/chat/UserMessage'
import {
  resolveMessageActions,
  setMessageActionOpener,
  setMessageActions,
  setMessageActionsLoadError,
  type MessageActionRequest,
} from '../messageActions/messageActionRegistry'

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../hooks/useSmoothStream', () => ({ useSmoothStream: (content: string) => content }))
vi.mock('../utils/shareUrl', () => ({ copySessionLink: vi.fn().mockResolvedValue(true) }))
vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(true) }))

const actions = resolveMessageActions([{
  name: 'notes-app',
  displayName: 'Notes',
  enabled: true,
  manifest: { contributes: { messageActions: [
    { id: 'save', title: 'Save to notes', entry: 'f.mjs' },
    { id: 'mine', title: 'Clip my message', entry: 'f.mjs' },
  ] } },
  messageActionGrant: { actions: [
    { id: 'save', title: 'Save to notes', roles: ['assistant'], format: 'plain', maxChars: 32000 },
    { id: 'mine', title: 'Clip my message', roles: ['user'], format: 'plain', maxChars: 32000 },
  ] },
}])

const opened: MessageActionRequest[] = []
beforeEach(() => {
  vi.useFakeTimers()
  opened.length = 0
  setMessageActions(actions)
  setMessageActionOpener((req) => { opened.push(req) })
})
afterEach(() => {
  act(() => { vi.runAllTimers() })
  vi.useRealTimers()
  setMessageActions([])
  setMessageActionOpener(null)
})

const REPLY = 'A completed reply that is comfortably longer than twenty characters.'
const open = (testId: string) => fireEvent.pointerDown(screen.getByTestId(testId), { button: 0, ctrlKey: false, pointerType: 'mouse' })

describe('message action menu rows', () => {
  it('say so, with a retry, when the app list failed to load', () => {
    setMessageActions([])
    const retry = vi.fn()
    setMessageActionsLoadError(retry)
    try {
      render(<AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} />)
      open('assistant-more-actions')
      expect(screen.queryByTestId('message-action-item')).toBeNull()
      expect(screen.getByRole('alert').textContent).toContain("Couldn't load app actions")
      fireEvent.click(screen.getByTestId('message-actions-retry'))
      expect(retry).toHaveBeenCalledTimes(1)
    } finally {
      setMessageActionsLoadError(null)
    }
  })
  it('offer an assistant-role action on a reply, labelled with its app, and hand over that reply', () => {
    render(<AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} />)
    open('assistant-more-actions')
    const items = screen.getAllByTestId('message-action-item')
    expect(items.map((i) => i.getAttribute('data-message-action'))).toEqual(['notes-app:save'])
    expect(items[0].textContent).toContain('Notes')
    expect(items[0].getAttribute('aria-label')).toContain('Notes')
    fireEvent.click(items[0])
    expect(opened).toHaveLength(1)
    expect(opened[0].source).toEqual({ markdown: REPLY, role: 'assistant' })
    expect(opened[0].anchor).toBe(screen.getByTestId('message-bubble'))
  })

  it('are not offered while a reply streams', () => {
    render(<AssistantMessage content={REPLY} isStreaming slotRunning />)
    expect(screen.queryByTestId('message-action-item')).toBeNull()
  })

  it('offer a user-role action only on the user\'s own message', () => {
    render(<UserMessage content="my own words" renderContent={(c: string) => <span>{c}</span>} messageTs="t1" slotKey="chat-1" onQuoteMessage={() => {}} />)
    open('user-more-actions')
    const items = screen.getAllByTestId('message-action-item')
    expect(items.map((i) => i.getAttribute('data-message-action'))).toEqual(['notes-app:mine'])
    fireEvent.click(items[0])
    expect(opened[0].source).toEqual({ markdown: 'my own words', role: 'user' })
  })

  it('leave the menus as they were when no app declares one', () => {
    setMessageActions([])
    render(<AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} />)
    // No other overflow item on this reply shape: no More at all.
    expect(screen.queryByTestId('assistant-more-actions')).toBeNull()
  })
})
