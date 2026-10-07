import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, ShieldAlert } from 'lucide-react'

import { api } from '../api/client'
import type { McpProjectTrustSnapshot } from '../api/client/mcp'
import { i18nT } from '../i18n/t'
import ErrorNotice from './ErrorNotice'
import Modal from './Modal'
import { Btn } from './ui'

// — Consent gate for a project's own MCP servers and hooks.
//
// A project's `.kiro/agents` spec can name MCP servers. On a Claude, Codex,
// Goose or OpenCode session each one is a command started as the operator,
// outside the tool sandbox, before any message is sent. The copy names that
// consequence plainly, and both choices say what happens. It is a separate
// consent from project skills: that one covers text entering context.

interface Props {
  open: boolean
  /** Real chat-slot key, so the server grants THIS chat's project. */
  slotKey?: string
  onClose: () => void
  onTrusted?: () => void
}

export default function ProjectMcpTrustDialog({ open, slotKey, onClose, onTrusted }: Props) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const queryClient = useQueryClient()

  const { data: trust, error: loadError } = useQuery<McpProjectTrustSnapshot>({
    // Same prefix as the settings list, so one invalidation refreshes both.
    queryKey: ['mcp-project-trust', slotKey ?? null],
    queryFn: () => api.mcpProjectTrust(slotKey),
    enabled: open,
  })
  const reviewedPath = trust?.project || null
  const reviewedKey = trust?.project_key || null
  const reviewedLaunch = trust?.launch && Object.keys(trust.launch).length > 0 ? trust.launch : null
  const servers = Array.isArray(trust?.servers) ? trust.servers : []
  const omitted = trust?.servers_omitted ?? 0
  const hooks = Array.isArray(trust?.hooks) ? trust.hooks : []
  const hooksOmitted = trust?.hooks_omitted ?? 0

  const confirm = async () => {
    if (!reviewedKey || !reviewedLaunch) return
    setPending(true)
    setError(null)
    try {
      const snapshot = await api.grantMcpProjectTrust(slotKey, reviewedKey, reviewedLaunch)
      await queryClient.invalidateQueries({ queryKey: ['mcp-project-trust'] })
      // Believe the response, not the absence of an exception.
      if (snapshot?.trusted !== true) {
        setError(i18nT('components.projectMcpTrust.grant_failed'))
        return
      }
      onTrusted?.()
      onClose()
    } catch (err: unknown) {
      const body = (err as { body?: unknown })?.body
      let detail = ''
      if (typeof body === 'string') {
        try {
          detail = String((JSON.parse(body) as { error?: string }).error ?? '')
        } catch {
          detail = ''
        }
      } else if (body && typeof body === 'object') {
        detail = String((body as { error?: string }).error ?? '')
      }
      setError(detail || i18nT('components.projectMcpTrust.grant_failed'))
    } finally {
      setPending(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      maxWidth={560}
      title={i18nT('components.projectMcpTrust.title')}
      footer={
        <>
          <Btn onClick={onClose} disabled={pending}>
            {i18nT('components.projectMcpTrust.decline')}
          </Btn>
          <Btn primary onClick={confirm} disabled={pending || !reviewedPath || !reviewedKey || !reviewedLaunch || !!trust?.too_many_specs}>
            {pending
              ? <><Loader2 size={14} className="animate-spin" /> {i18nT('components.projectMcpTrust.working')}</>
              : <><ShieldAlert size={14} /> {i18nT('components.projectMcpTrust.confirm')}</>}
          </Btn>
        </>
      }
    >
      <div className="flex flex-col gap-3.5 text-[13px]">
        <p>{i18nT('components.projectMcpTrust.body')}</p>
        <ul className="flex flex-col gap-1.5 max-h-[240px] overflow-y-auto">
          {servers.map(srv => (
            <li key={`${srv.agent}/${srv.name}`} className="rounded bg-card px-2 py-1.5">
              <div className="text-[12px] font-semibold text-text-strong break-all">{srv.name}</div>
              <code className="block break-all text-[12px] text-muted">
                {srv.url || [srv.command, ...srv.args].join(' ')}
                {(srv.args_omitted ?? 0) > 0 && ` ${i18nT('components.projectMcpTrust.args_omitted', { count: srv.args_omitted })}`}
              </code>
              {((srv.env_keys?.length ?? 0) > 0 || (srv.header_keys?.length ?? 0) > 0) && (
                <div className="text-[11px] text-muted break-all">
                  {i18nT('components.projectMcpTrust.masked_keys', {
                    keys: [...(srv.env_keys ?? []), ...(srv.header_keys ?? [])].join(', '),
                  })}
                </div>
              )}
            </li>
          ))}
        </ul>
        {omitted > 0 && (
          <p className="text-warn">
            {i18nT('components.projectMcpTrust.servers_omitted', { count: omitted })}
          </p>
        )}
        {hooks.length > 0 && (
          <>
            <p>{i18nT('components.projectMcpTrust.hooks_body')}</p>
            <ul className="flex flex-col gap-1.5 max-h-[200px] overflow-y-auto">
              {hooks.map((hook, i) => (
                <li key={`${hook.agent}/${hook.event}/${i}`} className="rounded bg-card px-2 py-1.5">
                  <div className="text-[12px] font-semibold text-text-strong break-all">
                    {hook.event}
                    {hook.matcher && (
                      <span className="font-normal text-muted"> {hook.matcher}</span>
                    )}
                    {hook.enabled === false && ` ${i18nT('components.projectMcpTrust.hook_off')}`}
                  </div>
                  <code className="block break-all text-[12px] text-muted">{hook.command}</code>
                </li>
              ))}
            </ul>
          </>
        )}
        {hooksOmitted > 0 && (
          <p className="text-warn">
            {i18nT('components.projectMcpTrust.hooks_omitted', { count: hooksOmitted })}
          </p>
        )}
        {trust?.too_many_specs && (
          <>
            {/* No hand-off: this dialog opens over the chat composer, whose unsent
                message draft the hand-off's navigation would discard. */}
            <ErrorNotice message={i18nT('components.projectMcpTrust.too_many_specs')} />
          </>
        )}
        {reviewedPath && (
          <code className="block break-all rounded bg-card px-2 py-1.5 text-[12px] text-muted">
            {reviewedPath}
          </code>
        )}
        <p className="text-warn">
          {i18nT('components.projectMcpTrust.consequence')}
        </p>
        <p className="text-warn font-semibold">
          {i18nT('components.projectMcpTrust.code_warning')}
        </p>
        <p className="text-muted">
          {i18nT('components.projectMcpTrust.separate')}
        </p>
        <p className="text-muted">
          {i18nT('components.projectMcpTrust.decline_consequence')}
        </p>
        <p className="text-muted">
          {i18nT('components.projectMcpTrust.withdraw_hint')}
        </p>
        {/* No hand-off: this dialog opens over the chat composer, whose unsent
            message draft the hand-off's navigation would discard. */}
        <ErrorNotice
          message={loadError ? i18nT('components.projectMcpTrust.load_failed') : null}
        />
        {/* No hand-off: same unsent composer draft as above. */}
        <ErrorNotice message={error} />
      </div>
    </Modal>
  )
}
