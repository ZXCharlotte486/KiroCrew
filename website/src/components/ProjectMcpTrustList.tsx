import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { FolderX, ShieldCheck } from 'lucide-react'

import { api } from '../api/client'
import type { McpProjectTrustSnapshot } from '../api/client/mcp'
import { i18nT } from '../i18n/t'
import ErrorNotice from './ErrorNotice'
import { Btn } from './ui'

// — Withdraw surface for project MCP server grants.
//
// The consent dialog promises a one-click withdraw; this list keeps that
// promise. It renders nothing when no grant exists. A grant whose folder is
// gone stays listed, so it stays revocable.

export default function ProjectMcpTrustList() {
  const queryClient = useQueryClient()
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const { data, error: loadError } = useQuery<McpProjectTrustSnapshot>({
    queryKey: ['mcp-project-trust'],
    // Defensive call: suites that partial-mock the api client leave a new
    // method undefined on a mount-time fetch.
    queryFn: () =>
      Promise.resolve(api.mcpProjectTrust?.()).then(r => (r ?? {}) as McpProjectTrustSnapshot),
    staleTime: 60 * 1000,
  })

  const grants = Array.isArray(data?.grants) ? data.grants : []
  // A failed read is not "no grants": hiding the list would hide the withdraw.
  if (loadError) {
    return (
      <ErrorNotice
        className="mb-3"
        askAgent
        message={i18nT('components.projectMcpTrustList.load_failed')}
      />
    )
  }
  if (grants.length === 0) return null

  const revoke = async (path: string) => {
    setBusy(path)
    setError(null)
    try {
      await api.revokeMcpProjectTrust(path)
      await queryClient.invalidateQueries({ queryKey: ['mcp-project-trust'] })
    } catch {
      setError(path)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="mb-3 rounded-lg border border-border p-3">
      <h4 className="text-sm font-semibold text-text-strong mb-1 flex items-center gap-2">
        <ShieldCheck size={14} className="lucide-inline" />
        {i18nT('components.projectMcpTrustList.title')}
      </h4>
      <p className="text-[11px] text-muted mb-2.5">
        {i18nT('components.projectMcpTrustList.hint')}
      </p>
      <ul className="flex flex-col gap-1.5">
        {grants.map(g => (
          <li key={g.path} className="flex items-center gap-3">
            <div className="min-w-0 flex-1">
              <div className="text-[12px] font-mono truncate text-text" title={g.path}>{g.path}</div>
              {g.exists === false && (
                <div className="text-[11px] text-muted flex items-center gap-1">
                  <FolderX size={11} className="lucide-inline" />
                  {i18nT('components.projectMcpTrustList.folder_missing')}
                </div>
              )}
              <ErrorNotice
                variant="inline"
                askAgent
                message={error === g.path ? i18nT('components.projectMcpTrustList.revoke_failed') : null}
              />
            </div>
            <Btn onClick={() => revoke(g.path)} disabled={busy === g.path}>
              {busy === g.path
                ? i18nT('components.projectMcpTrustList.revoking')
                : i18nT('components.projectMcpTrustList.revoke')}
            </Btn>
          </li>
        ))}
      </ul>
    </div>
  )
}
