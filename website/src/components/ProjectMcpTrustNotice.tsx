import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ShieldAlert } from 'lucide-react'

import { api } from '../api/client'
import type { McpProjectTrustSnapshot } from '../api/client/mcp'
import { i18nT } from '../i18n/t'
import ErrorNotice from './ErrorNotice'
import ProjectMcpTrustDialog from './ProjectMcpTrustDialog'
import { Btn } from './ui'

// — One-line notice above the composer when this chat's project declares MCP
// servers that stay off until the operator trusts them. Renders nothing when
// the project declares none or is already trusted. A non-owner's read is
// refused with `owner_only`, which is not a failure to report; any other read
// failure is shown, so the consent entry point never vanishes silently.

interface Props {
  /** Real chat-slot key, e.g. `dashboard:chat-7`. */
  slotKey?: string
  /** The chat's project path; part of the query key so a switch refetches. */
  project?: string
}

/** The owner gate's refusal: duck-typed so a partially mocked client still works. */
function isOwnerOnly(err: unknown): boolean {
  const e = err as { status?: unknown; body?: unknown }
  return e?.status === 403 && String(e?.body ?? '').includes('owner_only')
}

export default function ProjectMcpTrustNotice({ slotKey, project }: Props) {
  const [open, setOpen] = useState(false)
  const { data, error } = useQuery<McpProjectTrustSnapshot>({
    queryKey: ['mcp-project-trust', slotKey ?? null, project ?? null],
    queryFn: () =>
      Promise.resolve(api.mcpProjectTrust?.(slotKey)).then(r => (r ?? {}) as McpProjectTrustSnapshot),
    enabled: !!slotKey && !!project,
    retry: false,
    staleTime: 60 * 1000,
  })
  const servers = Array.isArray(data?.servers) ? data.servers : []
  const hooks = Array.isArray(data?.hooks) ? data.hooks : []
  const total =
    servers.length + (data?.servers_omitted ?? 0) + hooks.length + (data?.hooks_omitted ?? 0)
  if (!project) return null
  if (error) {
    if (isOwnerOnly(error)) return null
    return (
      <>
        {/* No hand-off: this notice sits directly above the chat composer, whose
            unsent message draft the hand-off's navigation would discard. */}
        <ErrorNotice className="mb-1" message={i18nT('components.projectMcpTrustNotice.load_failed')} />
      </>
    )
  }
  if (data?.too_many_specs) {
    return (
      <>
        {/* No hand-off: this notice sits directly above the chat composer, whose
            unsent message draft the hand-off's navigation would discard. */}
        <ErrorNotice className="mb-1" message={i18nT('components.projectMcpTrust.too_many_specs')} />
      </>
    )
  }
  if (data?.trusted !== false || total === 0) return null

  return (
    <>
      <div className="flex items-center gap-2 px-4 py-2 mb-1 bg-warn/10 rounded-lg text-[13px]">
        <ShieldAlert size={14} className="lucide-inline text-warn shrink-0" />
        <span className="min-w-0 flex-1">
          {i18nT('components.projectMcpTrustNotice.text', { count: total })}
        </span>
        <Btn onClick={() => setOpen(true)}>
          {i18nT('components.projectMcpTrustNotice.review')}
        </Btn>
      </div>
      <ProjectMcpTrustDialog open={open} slotKey={slotKey} onClose={() => setOpen(false)} />
    </>
  )
}
