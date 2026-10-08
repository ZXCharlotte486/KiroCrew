import { useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../../api/client'
import { fmtList } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import { useAppSelector } from '../../../store'
import type { SubagentActivity } from '../../../types'
import { baselineOrHeld } from '../../../hooks/useWebSocket'
import { buildCommandCenter, effectiveApprovalMode, scopedSlots, slotKey, type PendingQuestion, type WorkItem } from './model'

/** The work board as its route serves it: the folded value plus its revision. */
type WorkProjection = { value?: { items: WorkItem[]; omitted?: number }; revision?: number }

/** Names of the optional sources that can fail. Literal keys, so the catalog
 * tooling sees every one. */
const MISSING_SOURCE_KEYS = {
  runs: 'commandCenter.source_runs',
  work: 'commandCenter.source_work',
} as const

/** One notice naming every optional source that failed, so the reassurance is
 * said once however many are missing; null when none is. */
export function missingSourcesNotice(missing: readonly (keyof typeof MISSING_SOURCE_KEYS)[]): string | null {
  if (!missing.length) return null
  return i18nT('commandCenter.partial_sources', { sources: fmtList(missing.map(name => i18nT(MISSING_SOURCE_KEYS[name]))) })
}
const EMPTY_AGENTS: Record<string, SubagentActivity> = {}

/** Shared query keys let the panel and the fleet page observe one read, not one per
 * worker. Nothing here polls: every source is refreshed by the frame that announces
 * its change (`approval*`, `question_card*`, the crew log's `slot_projection` for the
 * work board, workflow events into the store) and all of them again on reconnect, so
 * an open chat tab costs no periodic requests. */
export function useCommandCenter(root: string | null, enabled = true, scope: 'task' | 'fleet' = 'task') {
  const slots = useAppSelector(s => s.dashboard.slots)
  const approvalMode = useAppSelector(s => s.dashboard.approvalMode)
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const liveAgents = useAppSelector(s => s.chat.subagents)
  const background = useAppSelector(s => s.chat.slotActivity)
  const liveWorkflows = useAppSelector(s => s.chat.workflowRuns)
  const connected = useAppSelector(s => s.dashboard.connected)
  const fleet = scope === 'fleet'
  // Read inside the work query's own `queryFn`, to compare a baseline response
  // against the value a push may already have put in this key.
  const queryClient = useQueryClient()
  const scoped = useMemo(() => fleet ? slots : root ? scopedSlots(slots, root) : [], [slots, root, fleet])
  const canRead = enabled && (fleet || !!root && scoped.length > 0)
  // The app-wide policy is never-stale; a finite staleTime here made every
  // window focus re-read all of these. Frames and reconnect own freshness; a
  // failed source also re-reads on focus, since its frame may never come.
  const sourceOptions = {
    enabled: canRead, staleTime: 3_000,
    refetchOnWindowFocus: (query: { state: { status: string } }) => query.state.status === 'error',
  }
  const questions = useQuery({ queryKey: ['command-center', 'questions'], queryFn: api.pendingQuestions, ...sourceOptions })
  // Only the mounted owner's actively drafted STATELESS cards survive retirement.
  // This is presentation continuity, never a cache of live approval/ask authority.
  const draftScope = JSON.stringify([scope, root])
  const [drafts, setDrafts] = useState<{ scope: string; cards: Record<string, PendingQuestion> }>({ scope: draftScope, cards: {} })
  if (drafts.scope !== draftScope) setDrafts({ scope: draftScope, cards: {} })
  /* WHICH cards are being typed into, as ids only. Deliberately separate from
     `drafts.cards` above, because the two answer different questions and only one
     of them may include a blocking ask:
       - `drafts.cards` RETAINS a card past its retirement, so it is limited to
         stateless `card_id` questions. Retaining a blocking `ask_id` question
         would resurrect a card whose authority the live list owns.
       - this set only says "text is unsent", which is true of a blocking ask too,
         and a HOST uses it to keep the subtree mounted.
     Folding the second into the first is what made a blocking ask's typed answer
     unprotected: its early return left the flag false, the host released the
     panel, and the draft went with the unmount. */
  const [draftIds, setDraftIds] = useState<{ scope: string; ids: string[] }>({ scope: draftScope, ids: [] })
  if (draftIds.scope !== draftScope) setDraftIds({ scope: draftScope, ids: [] })
  const onQuestionDraftChange = (question: PendingQuestion, active: boolean) => {
    // A trailing `[OPTIONS:]` ask (`followUp`) has neither id, but a pick in it
    // is unsent text all the same; one per session is all the model ever offers.
    const key = question.ask_id || question.card_id || (question.followUp ? 'follow-up' : '')
    if (!key) return
    const draftId = JSON.stringify([slotKey(question.slot), key])
    setDraftIds(previous => {
      // A departing card's cleanup must not clear a new scope's draft.
      if (previous.scope !== draftScope) return previous
      const held = previous.ids.includes(draftId)
      if (active === held) return previous
      return { ...previous, ids: active ? [...previous.ids, draftId] : previous.ids.filter(id => id !== draftId) }
    })
    if (question.ask_id || !question.card_id) return
    const id = JSON.stringify([slotKey(question.slot), question.card_id])
    setDrafts(previous => {
      if (previous.scope !== draftScope) return previous
      if (active) return previous.cards[id] === question ? previous : { ...previous, cards: { ...previous.cards, [id]: question } }
      if (!previous.cards[id]) return previous
      const cards = { ...previous.cards }
      delete cards[id]
      return { ...previous, cards }
    })
  }
  const visibleQuestions = useMemo(() => {
    const live = questions.data || []
    const ids = new Set(live.map(q => JSON.stringify([slotKey(q.slot), q.card_id])))
    return [...live, ...Object.values(drafts.scope === draftScope ? drafts.cards : {}).filter(q => !ids.has(JSON.stringify([slotKey(q.slot), q.card_id])))]
  }, [questions.data, drafts, draftScope])
  // The same inventory the app shell already keeps: one cache, one request per frame.
  const approvals = useQuery({ queryKey: ['global-approvals'], queryFn: () => api.approvals(), ...sourceOptions })
  const workflows = useQuery({ queryKey: ['command-center', 'workflows'], queryFn: api.workflowRuns, ...sourceOptions })
  const work = useQuery({
    queryKey: ['command-center', root, 'work'],
    // Gated on the revision floor the socket handed this tab before any read went
    // out: a response at or below it describes an older fold than one already
    // applied here, and letting it land would undo a push by arriving later.
    queryFn: async () => {
      const key = ['command-center', root, 'work']
      const response = (await api.sessionWorkProjection(root!)) as WorkProjection
      return baselineOrHeld(root!, 'work', response, queryClient.getQueryData<WorkProjection>(key))
    },
    ...sourceOptions, enabled: canRead && !fleet,
  })
  const model = useMemo(() => {
    const subagents = Object.fromEntries(scoped.map(s => [s.key,
      s.key === activeSlot ? liveAgents : background?.[s.key]?.subagents || EMPTY_AGENTS,
    ]))
    // REST restores completed runs after a reload; live events win until the next
    // authoritative snapshot read. Neither an unavailable endpoint nor an idle slot is success.
    const runs = new Map((workflows.data?.runs || []).map(r => [r.run_id, r]))
    for (const r of Object.values(liveWorkflows || {})) {
      runs.set(r.run_id, { ...runs.get(r.run_id), run_id: r.run_id, name: r.name, status: r.status,
        session_key: r.sessionKey || runs.get(r.run_id)?.session_key || '', error: r.error })
    }
    return buildCommandCenter({ root: fleet ? null : root, slots: scoped, subagents, approvalMode, workflows: [...runs.values()],
      questions: visibleQuestions, approvals: approvals.data || [], work: work.isEnabled ? work.data?.value : undefined })
  }, [root, scoped, activeSlot, liveAgents, background, liveWorkflows, workflows.data, visibleQuestions, approvals.data, work.data, work.isEnabled, approvalMode, fleet])
  const sources = [questions, approvals, workflows, ...(work.isEnabled ? [work] : [])]
  // Questions and approvals are what a person must act on; the rest decorate.
  // An optional source failing (workflows answer 503 while their service starts)
  // must not hide fresh decisions behind a stale notice.
  const required = [questions, approvals]
  const loading = canRead && sources.some(q => q.isPending)
  const stale = canRead && (!connected || required.some(q => q.isError))
  // Which optional source failed while decisions are fresh, so the notice can
  // name what is missing rather than hand the person a vague uncertainty. Only
  // once both decision reads have answered, since the notice vouches for them.
  const missing = canRead && connected && required.every(q => q.isSuccess)
    ? ([['runs', workflows], ['work', work]] as const)
      .filter(([, q]) => q.isEnabled && q.isError).map(([name]) => name)
    : []
  // Whether ANY card on this scope is holding a half-entered answer -- a blocking
  // ask as much as a stateless one. Read as one boolean so a HOST can keep its
  // panel mounted while text is unsent: the Crewmates page does, because that
  // draft lives nowhere but component state and an unmount is the text being
  // thrown away. Taken from `draftIds`, not from the retention map, for the reason
  // given where they are declared. Scope-guarded like the reads above: a departing
  // scope's cards never answer for the new one.
  const hasQuestionDraft = draftIds.scope === draftScope && draftIds.ids.length > 0
  return {
    ...model, connected, onQuestionDraftChange, hasQuestionDraft,
    approvalMode: effectiveApprovalMode(approvalMode, slots.find(s => s.key === root)),
    loading, stale, missing,
    // Real clock from completed reads. A websocket connection alone doesn't
    // establish that a server-side question/approval inventory is up to date.
    updatedAt: Math.min(...required.map(q => q.dataUpdatedAt)),
    approvalCount: model.attention.filter(a => a.kind === 'approval').length,
  }
}

export type CommandCenterData = ReturnType<typeof useCommandCenter>
