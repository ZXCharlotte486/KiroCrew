/**
 * The text a message action is handed: the message as the reader sees it.
 *
 * By default the RENDERED plain text, not the markdown source. The reply is
 * cut into the chat's own blocks, so a complete markdown fence reads as the
 * formatted card the chat shows and a diagram or widget gives nothing. Prose is
 * read off the same sanitized tree the chat renders: the chat's remark chain, raw HTML
 * parsed by `rehype-raw`, then the chat's own `rehypeSanitize`. So a link gives
 * its label and never its address, an image only its alt text (never its URL,
 * which the chat shows behind a click-to-load disclosure), a raw `<a href>` or
 * `<img src>` the chat renders is reduced the same way, a tag the chat shows as
 * literal source stays literal text, and markup the renderer drops stays
 * dropped. Markdown is a separate, approved format. Either way the text is cut
 * to the action's `maxChars` in UTF-16 units, never inside a surrogate pair.
 *
 * @module messageActions/messageText
 */
import { unified, type PluggableList } from 'unified'
import remarkParse from 'remark-parse'
import { toHast } from 'mdast-util-to-hast'
import type { Nodes as HastNodes, Root as HastRoot } from 'hast'
import type { Root as MdastRoot } from 'mdast'
import { chatProsePipeline } from '../components/MarkdownRenderer'
import { isMarkdownBlock } from '../components/markdown/markdownSniff'
import { parseBlocks } from '../hooks/useBlockAssembler'
import type { ContentBlock } from '../types'

// The chat's own chains, shared rather than copied, so a pass the chat adds is a
// pass this text gets too. Built once, on first use.
type Chains = { remark: ReturnType<typeof makeRemark>; rehype: ReturnType<typeof makeRehype> }
const makeRemark = (plugins: PluggableList) => unified().use(remarkParse).use(plugins)
const makeRehype = (plugins: PluggableList) => unified().use(plugins)
let chains: Chains | null = null
function chainsFor(remark: PluggableList, rehype: PluggableList): Chains {
  chains ??= { remark: makeRemark(remark), rehype: makeRehype(rehype) }
  return chains
}

const BREAK = '\u0000'
const BREAK_RUN = /\u0000\n?/g

/** Elements that end a line of their own. */
const BLOCKS = new Set([
  'p', 'div', 'section', 'article', 'aside', 'header', 'footer', 'nav', 'main', 'figure', 'figcaption',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre', 'ul', 'ol', 'li', 'dl', 'dt', 'dd',
  'table', 'thead', 'tbody', 'tfoot', 'tr', 'caption', 'hr', 'details', 'summary',
])

function walk(node: HastNodes, out: string[]): void {
  switch (node.type) {
    case 'text':
      out.push(node.value)
      return
    case 'comment':
    case 'doctype':
      return
    case 'element': {
      const tag = node.tagName.toLowerCase()
      if (tag === 'img') {
        const alt = node.properties?.alt
        out.push(typeof alt === 'string' ? alt : '')
        return
      }
      if (tag === 'br') {
        // A sentinel, not '\n': the tree holds a '\n' text node right after a
        // hard break, and the break plus that node is still one line end.
        // CommonMark replaces NUL in the source, so the sentinel is unambiguous.
        out.push(BREAK)
        return
      }
      if (tag === 'td' || tag === 'th') out.push('\t')
      const block = BLOCKS.has(tag)
      if (block) out.push('\n')
      for (const child of node.children) walk(child, out)
      if (block) out.push('\n')
      return
    }
    case 'root':
      for (const child of node.children) walk(child, out)
      return
    default:
      // Any node type the sanitizer let through but that is not text (e.g. `raw`) shows nothing.
      return
  }
}

/** One prose block (no top-level fences) reduced to its rendered text. */
function proseText(markdown: string): string {
  const { source, remark: remarkPlugins, rehype: rehypePlugins } = chatProsePipeline(markdown)
  const { remark, rehype } = chainsFor(remarkPlugins, rehypePlugins)
  const mdast = remark.runSync(remark.parse(source)) as MdastRoot
  const hast = rehype.runSync(toHast(mdast, { allowDangerousHtml: true }) as HastRoot) as HastRoot
  const out: string[] = []
  walk(hast, out)
  return tidy(out.join('').replace(BREAK_RUN, '\n'))
}

function tidy(text: string): string {
  return text
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n\t/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

/** One chat block reduced the way `BlockRenderer` shows it. */
function blockText(block: ContentBlock): string {
  switch (block.type) {
    case 'markdown':
      return proseText(block.content)
    case 'code':
      // A complete markdown fence is a formatted card in the chat: its text, not its source.
      return block.complete && isMarkdownBlock(block.language, block.content) ? proseText(block.content) : block.content
    case 'diff':
      return block.content
    // A diagram or a widget is drawn, not shown as text: its source can carry
    // addresses and markup the reader never sees, so it gives nothing.
    default:
      return ''
  }
}

/**
 * The rendered plain text of a markdown message. The reply is cut into the
 * chat's own blocks (`parseBlocks`, the splitter the chat renders from), and
 * each block is reduced the way the chat draws it.
 */
export function renderedPlainText(markdown: string): string {
  return tidy(parseBlocks(markdown, false).map(blockText).filter((t) => t.trim()).join('\n\n'))
}

/** `text` cut to at most `max` UTF-16 units, never splitting a surrogate pair. */
export function truncateUnits(text: string, max: number): { text: string; truncated: boolean } {
  if (text.length <= max) return { text, truncated: false }
  let end = max
  const code = text.charCodeAt(end - 1)
  if (code >= 0xd800 && code <= 0xdbff) end -= 1
  return { text: text.slice(0, end), truncated: true }
}

/** The text for one invocation, in the action's approved format. */
export function messageTextFor(markdown: string, format: 'plain' | 'markdown', maxChars: number): { text: string; truncated: boolean } {
  return truncateUnits(format === 'markdown' ? markdown : renderedPlainText(markdown), maxChars)
}
