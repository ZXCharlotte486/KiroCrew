/**
 * Message actions: what an app sees on a click, and what it never sees.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { APP_FRAME_TAG } from '../appFrame/frameProtocol'
import { FRAME_SANDBOX } from '../appFrame/sandboxFrame'
import { installFrameWindows, postFrom, sentTo, type Sent } from '../test/LinkPreviewFramesTestUtils'
import { INVOCATION_DEADLINE_MS, MessageActionPopover, READY_TIMEOUT_MS, setMessageActionMintForTest, type CancelReason } from './MessageActionLayer'
import { messageActionGrantLabel, resolveMessageActions, type ResolvedMessageAction } from './messageActionRegistry'
import { messageTextFor, renderedPlainText, truncateUnits } from './messageText'

const copyToClipboard = vi.fn(async (_text: string) => true)
vi.mock('../utils/clipboard', () => ({ copyToClipboard: (t: string) => copyToClipboard(t) }))
const openExternal = vi.fn()
vi.mock('../hooks/useFrameOpenLink', async (orig) => ({ ...(await orig<object>()), openExternal: (u: string) => openExternal(u) }))

const app = (over: Record<string, unknown> = {}) => ({
  name: 'notes-app',
  displayName: 'Notes',
  enabled: true,
  version: '1.0.0',
  manifest: { contributes: { messageActions: [{ id: 'save', title: 'Save to notes', entry: 'dist/frame.mjs' }] } },
  messageActionGrant: {
    actions: [{ id: 'save', title: 'Save to notes', roles: ['assistant'], format: 'plain', maxChars: 32000, popover: { width: 360, maxHeight: 480 } }],
    staged: [],
  },
  ...over,
})

describe('resolveMessageActions', () => {
  it('offers only approved, still-declared actions of enabled apps', () => {
    expect(resolveMessageActions([app()]).map((a) => a.key)).toEqual(['notes-app:save'])
    expect(resolveMessageActions([app({ enabled: false })])).toEqual([])
    // Declared but not approved: the grant row is the authority.
    expect(resolveMessageActions([app({ messageActionGrant: { actions: [] } })])).toEqual([])
    expect(resolveMessageActions([app({ messageActionGrant: undefined })])).toEqual([])
    // Approved but no longer declared (or declared with a bad entry).
    expect(resolveMessageActions([app({ manifest: { contributes: { messageActions: [] } } })])).toEqual([])
    expect(resolveMessageActions([app({ manifest: { contributes: { messageActions: [{ id: 'save', entry: '../x.mjs' }] } } })])).toEqual([])
  })

  it('caps actions per app, clamps bounds and orders by app name', () => {
    const rows = ['a', 'b', 'c'].map((id) => ({ id, title: id.toUpperCase(), roles: ['assistant', 'user', 'bogus'], format: 'markdown', maxChars: 99999, popover: { width: 9000, maxHeight: 1 } }))
    const many = app({
      name: 'zeta', displayName: 'Zeta',
      manifest: { contributes: { messageActions: rows.map((r) => ({ id: r.id, entry: 'f.mjs' })) } },
      messageActionGrant: { actions: rows },
    })
    const out = resolveMessageActions([many, app()])
    expect(out.map((a) => a.key)).toEqual(['notes-app:save', 'zeta:a', 'zeta:b'])
    const z = out[1]
    expect(z.roles).toEqual(['assistant', 'user'])
    expect([z.format, z.maxChars, z.width, z.maxHeight]).toEqual(['markdown', 32000, 480, 120])
  })

  it('names a held-back entry in plain words', () => {
    const declared = [{ id: 'save', title: 'Save to notes' }]
    expect(messageActionGrantLabel('save:role:user', declared)).toContain('Save to notes')
    expect(messageActionGrantLabel('save:format:markdown', declared)).toContain('markdown')
    expect(messageActionGrantLabel('weird', declared)).toBe('weird')
  })
})

describe('message text', () => {
  it('is the rendered text: a link gives its label, not its address', () => {
    const text = renderedPlainText('# Title\n\nSee [the doc](https://secret.example.com/?token=abc) and **bold**.\n\n- one\n- two')
    expect(text).toContain('Title')
    expect(text).toContain('See the doc and bold.')
    expect(text).not.toContain('secret.example.com')
    expect(text).not.toContain('**')
    expect(text).toMatch(/one\s+two/)
  })
  it('gives an image its alt text only, never its address', () => {
    const text = renderedPlainText('Look: ![chart](https://images.example/p.png?sig=abc) and ![](https://images.example/q.png)')
    expect(text).toBe('Look: chart and')
    expect(text).not.toContain('images.example')
  })
  it('reduces raw HTML the chat renders to what it shows: a label, an alt, never an address', () => {
    const text = renderedPlainText('See <a href="https://docs.example/private?token=x">docs</a> and <img src="https://img.example/p.png" alt="plot">.\n\n<div>\n<b>Bold</b> <a href="https://deep.example/">deep</a>\n</div>')
    expect(text).toContain('See docs and plot.')
    expect(text).toContain('Bold deep')
    expect(text).not.toMatch(/docs\.example|img\.example|deep\.example|href|src=|<a|<img|<div/)
  })
  it('keeps a tag the chat shows as literal source, and drops a comment', () => {
    const text = renderedPlainText('Use <customTag>x</customTag> here.<!-- hidden note -->')
    expect(text).toContain('<customTag>x</customTag>')
    expect(text).not.toContain('hidden note')
  })
  it('keeps code, line breaks and table cells, and drops reference definitions', () => {
    const text = renderedPlainText('```js\nconst a = 1\n```\n\nline one  \nline two\n\n| h1 | h2 |\n| --- | --- |\n| c1 | c2 |\n\n[ref]: https://ref.example/')
    expect(text).toContain('const a = 1')
    expect(text).toContain('line one\nline two')
    expect(text).toMatch(/h1\th2/)
    expect(text).toMatch(/c1\tc2/)
    expect(text).not.toContain('ref.example')
  })
  it('hands markdown over unchanged only in the markdown format, and caps either', () => {
    const md = 'A [link](https://x.example/)'
    expect(messageTextFor(md, 'markdown', 100)).toEqual({ text: md, truncated: false })
    expect(messageTextFor(md, 'plain', 100)).toEqual({ text: 'A link', truncated: false })
    expect(messageTextFor(md, 'plain', 3)).toEqual({ text: 'A l', truncated: true })
  })
  it('reads a link the chat repairs as its label, as the chat shows it', () => {
    const text = renderedPlainText('File [q](https://x.example/new?title=a b&labels=bug) now.')
    expect(text).toBe('File q now.')
  })
  it('reads a complete markdown fence as the formatted card the chat shows', () => {
    for (const lang of ['markdown', 'md', 'mdx']) {
      const text = renderedPlainText(`Intro\n\n\`\`\`${lang}\n# Doc\n\nSee [docs](https://example.com/private) and ![p](https://img.example/p.png)\n\`\`\`\n\nAfter`)
      expect(text).toContain('Intro')
      expect(text).toContain('See docs and p')
      expect(text).toContain('After')
      expect(text).not.toMatch(/example\.com|img\.example|# Doc/)
    }
    // An untagged fence that is clearly markdown is a card too.
    const sniffed = renderedPlainText('```\n# Title\n\n- [one](https://a.example/)\n- **two**\n```')
    expect(sniffed).not.toContain('a.example')
    expect(sniffed).toContain('one')
  })
  it('keeps a real code fence and a diff as written, and gives nothing for a diagram or widget', () => {
    const text = renderedPlainText('```python\nurl = "https://api.example/"\n```\n\n```diff\n-a\n+b\n```\n\n```mermaid\ngraph TD; A-->B; click A "https://hidden.example/"\n```\n\n<mcwidget title="w">\n```html\n<a href="https://widget.example/">x</a>\n```\n</mcwidget>')
    expect(text).toContain('url = "https://api.example/"')
    expect(text).toContain('-a\n+b')
    expect(text).not.toMatch(/hidden\.example|widget\.example|graph TD/)
  })
  it('never cuts a surrogate pair', () => {
    expect(truncateUnits('ab\u{1F4DD}c', 3)).toEqual({ text: 'ab', truncated: true })
    expect(truncateUnits('abc', 3)).toEqual({ text: 'abc', truncated: false })
  })
})

describe('MessageActionPopover', () => {
  beforeAll(() => installFrameWindows())
  const action: ResolvedMessageAction = resolveMessageActions([app()])[0]
  const mint = vi.fn(async () => ({ frame: '/app-frame/notes-app/messageActions/save/fid/tok', frameId: 'fid', capability: 'cap-1', ttl: 900 }))
  beforeEach(() => {
    setMessageActionMintForTest(mint)
    copyToClipboard.mockClear()
    openExternal.mockClear()
    mint.mockClear()
  })
  afterEach(() => {
    setMessageActionMintForTest()
    vi.useRealTimers()
  })

  async function openFrame(onClose: (r?: CancelReason) => void = vi.fn(), markdown = 'Hello [docs](https://secret.example.com/x)') {
    const anchor = document.createElement('div')
    anchor.textContent = 'bubble'
    document.body.appendChild(anchor)
    const other = document.createElement('div')
    other.textContent = 'Other message with a secret'
    document.body.appendChild(other)
    const reason: { current: CancelReason } = { current: 'closed' }
    const view = render(<MessageActionPopover req={{ action, source: { markdown, role: 'assistant' }, anchor }} reasonRef={reason} onClose={onClose} />)
    const el = await waitFor(() => {
      const f = document.querySelector<HTMLIFrameElement>('iframe[data-message-action-frame]')
      if (!f) throw new Error('no frame yet')
      return f
    })
    await waitFor(() => expect(sentTo(el)[0]).toMatchObject({ type: 'init' }))
    return { el, view, onClose }
  }
  const from = (el: HTMLIFrameElement, type: string, data: Sent = {}) =>
    act(() => postFrom({ [APP_FRAME_TAG]: 1, type, ...data }, el.contentWindow))
  const invokeOf = (el: HTMLIFrameElement) => sentTo(el).find((m) => m.type === 'message.invoke') as Sent

  it('loads the shared sandbox and hands over this message only, as rendered text', async () => {
    const { el } = await openFrame()
    expect(el.getAttribute('sandbox')).toBe(FRAME_SANDBOX)
    expect(el.getAttribute('src')).toBe('/app-frame/notes-app/messageActions/save/fid/tok')
    expect(sentTo(el)[0]).toMatchObject({ type: 'init', capability: 'cap-1' })
    from(el, 'ready')
    const inv = invokeOf(el)
    expect(inv).toMatchObject({ text: 'Hello docs', format: 'plain', truncated: false, role: 'assistant', deadlineMs: INVOCATION_DEADLINE_MS })
    expect(typeof inv.invocationId).toBe('string')
    const everything = JSON.stringify(sentTo(el))
    expect(everything).not.toContain('secret')
    expect(everything).not.toContain('Other message')
    expect(screen.getByTestId('message-action-attribution').textContent).toContain('Notes')
  })

  it('acks a current offer, refuses a stale or out-of-order one, and copies only on click', async () => {
    const { el } = await openFrame()
    from(el, 'ready')
    const id = invokeOf(el).invocationId
    from(el, 'result.offer', { invocationId: id, seq: 1, kind: 'copy', value: 'note link' })
    expect(sentTo(el).at(-1)).toEqual(expect.objectContaining({ type: 'result.ack', invocationId: id, seq: 1 }))
    expect(copyToClipboard).not.toHaveBeenCalled()
    from(el, 'result.offer', { invocationId: id, seq: 1, kind: 'copy', value: 'again' })
    expect(sentTo(el).at(-1)).toEqual(expect.objectContaining({ type: 'result.error', code: 'stale' }))
    from(el, 'result.offer', { invocationId: 'forged', seq: 2, kind: 'copy', value: 'x' })
    expect(sentTo(el).at(-1)).toEqual(expect.objectContaining({ type: 'result.error', code: 'stale' }))
    from(el, 'result.offer', { invocationId: id, seq: 3, kind: 'open', value: 'javascript:alert(1)' })
    expect(sentTo(el).at(-1)).toEqual(expect.objectContaining({ type: 'result.error', code: 'not_allowed' }))
    fireEvent.click(screen.getByTestId('message-action-copy'))
    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledWith('note link'))
    expect(screen.queryByTestId('message-action-open')).toBeNull()
    from(el, 'result.offer', { invocationId: id, seq: 4, kind: 'open', value: 'https://notes.example.com/n/1' })
    const open = screen.getByTestId('message-action-open')
    expect(open.textContent).toContain('notes.example.com')
    fireEvent.click(open)
    expect(openExternal).toHaveBeenCalledWith('https://notes.example.com/n/1')
  })

  it('does not mark a replacement offer copied when the earlier write settles late', async () => {
    const { el } = await openFrame()
    from(el, 'ready')
    const id = invokeOf(el).invocationId
    from(el, 'result.offer', { invocationId: id, seq: 1, kind: 'copy', value: 'A' })
    let finish: (ok: boolean) => void = () => {}
    copyToClipboard.mockImplementationOnce(() => new Promise<boolean>((resolve) => { finish = resolve }))
    fireEvent.click(screen.getByTestId('message-action-copy'))
    expect(copyToClipboard).toHaveBeenCalledWith('A')
    from(el, 'result.offer', { invocationId: id, seq: 2, kind: 'copy', value: 'B' })
    await act(async () => { finish(true) })
    expect(screen.getByTestId('message-action-copy').textContent).toBe('Copy')
    fireEvent.click(screen.getByTestId('message-action-copy'))
    await waitFor(() => expect(screen.getByTestId('message-action-copy').textContent).toBe('Copied'))
    expect(copyToClipboard).toHaveBeenLastCalledWith('B')
  })

  it('stays open for a press on a menu, closes for a press elsewhere', async () => {
    const onClose = vi.fn()
    await openFrame(onClose)
    const menu = document.createElement('div')
    menu.setAttribute('role', 'menu')
    const item = document.createElement('div')
    menu.appendChild(item)
    const trigger = document.createElement('button')
    trigger.setAttribute('aria-haspopup', 'menu')
    document.body.append(menu, trigger)
    fireEvent.pointerDown(item)
    fireEvent.pointerDown(trigger)
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.pointerDown(document.body)
    expect(onClose).toHaveBeenCalledWith('closed')
  })

  it('never grows past the viewport, and says so when the clipboard refuses', async () => {
    const { el } = await openFrame()
    expect(screen.getByTestId('message-action-popover').style.maxHeight).toBe('calc(100vh - 16px)')
    from(el, 'ready')
    const id = invokeOf(el).invocationId
    from(el, 'result.offer', { invocationId: id, seq: 1, kind: 'copy', value: 'x' })
    copyToClipboard.mockResolvedValueOnce(false)
    fireEvent.click(screen.getByTestId('message-action-copy'))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain("Couldn't copy"))
  })

  it('cancels the open invocation when the popover closes', async () => {
    const { el, view } = await openFrame()
    from(el, 'ready')
    const id = invokeOf(el).invocationId
    const sent = sentTo(el)
    view.unmount()
    expect(sent.find((m) => m.type === 'invoke.cancel')).toEqual(expect.objectContaining({ invocationId: id, reason: 'closed' }))
    expect(sent.at(-1)).toEqual(expect.objectContaining({ type: 'dispose' }))
    expect(document.querySelector('iframe[data-message-action-frame]')).toBeNull()
  })

  it('ends the invocation at its deadline, which each resize extends', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const onClose = vi.fn()
    const { el } = await openFrame(onClose)
    from(el, 'ready')
    const id = invokeOf(el).invocationId
    act(() => { vi.advanceTimersByTime(INVOCATION_DEADLINE_MS - 1000) })
    from(el, 'invocation.resize', { invocationId: id, width: 300, height: 9999 })
    act(() => { vi.advanceTimersByTime(INVOCATION_DEADLINE_MS - 1000) })
    expect(onClose).not.toHaveBeenCalled()
    act(() => { vi.advanceTimersByTime(2000) })
    expect(onClose).toHaveBeenCalledWith('deadline')
  })

  it('says the app did not respond when the frame never gets ready', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { el } = await openFrame()
    act(() => { vi.advanceTimersByTime(READY_TIMEOUT_MS + 10) })
    expect(screen.getByTestId('message-action-failed').textContent).toContain('Notes')
    from(el, 'ready')
    expect(invokeOf(el)).toBeUndefined()
  })

  it('ignores messages from any other window, and a close for another invocation', async () => {
    const onClose = vi.fn()
    const { el } = await openFrame(onClose)
    act(() => postFrom({ [APP_FRAME_TAG]: 1, type: 'ready' }, window))
    expect(invokeOf(el)).toBeUndefined()
    from(el, 'ready')
    from(el, 'invocation.close', { invocationId: 'other' })
    expect(onClose).not.toHaveBeenCalled()
    from(el, 'invocation.close', { invocationId: invokeOf(el).invocationId })
    expect(onClose).toHaveBeenCalledWith('closed')
  })
})
