/**
 * The Shared Crew card on Settings > Developer > Feature Previews: the owner's
 * stored choice behind `/api/shared-crew/preview`, which takes effect only after
 * a gateway restart.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import type { SharedCrewPreviewSetting } from '../../api/sharedCrew'
import { FeaturePreviewsSection } from './FeaturePreviewsSection'

vi.mock('../../api/client', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      sharedCrewPreview: vi.fn(),
      sharedCrewSetPreview: vi.fn(),
      restartGateway: vi.fn(),
      dashboardConfig: vi.fn(),
      kirocrewConfig: vi.fn(),
      getDecisionsConsent: vi.fn(),
    },
  }
})
const read = vi.mocked(api.sharedCrewPreview)
const write = vi.mocked(api.sharedCrewSetPreview)
const restart = vi.mocked(api.restartGateway)

const setting = (over: Partial<SharedCrewPreviewSetting> = {}): SharedCrewPreviewSetting => ({
  enabled: false, active: false, env_override: false, restart_required: false, ...over,
})

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><FeaturePreviewsSection /></MemoryRouter>
    </QueryClientProvider>,
  )
}
const toggle = () => screen.findByRole('switch', { name: 'Shared Crew' })

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api.dashboardConfig).mockResolvedValue({ decisions_enabled: false } as never)
  vi.mocked(api.kirocrewConfig).mockResolvedValue({} as never)
  vi.mocked(api.getDecisionsConsent).mockResolvedValue({ enabled: false, endpoint: '', configured_endpoint: '', permits: false })
})

describe('Shared Crew preview card', () => {
  it('shows the stored choice and writes {enabled} through the preview route', async () => {
    read.mockResolvedValue(setting())
    write.mockResolvedValue(setting({ enabled: true, restart_required: true }))
    mount()
    const sw = await toggle()
    expect(sw).toHaveAttribute('aria-checked', 'false')
    expect(screen.queryByTestId('shared-crew-preview-restart')).toBeNull()
    fireEvent.click(sw)
    await waitFor(() => expect(write).toHaveBeenCalledWith(true))
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Shared Crew' })).toHaveAttribute('aria-checked', 'true'))
  })

  it('a stored choice that differs from what the gateway started with asks for a restart, with the restart action', async () => {
    read.mockResolvedValue(setting({ enabled: true, active: false, restart_required: true }))
    restart.mockResolvedValue({} as never)
    mount()
    const notice = await screen.findByTestId('shared-crew-preview-restart')
    expect(notice).toHaveTextContent('Restart the gateway to apply')
    fireEvent.click(screen.getByTestId('shared-crew-preview-restart-button'))
    await waitFor(() => expect(restart).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByTestId('shared-crew-preview-restart-button')).toHaveTextContent('Restarting…'))
    expect(screen.getByTestId('shared-crew-preview-restart-button')).toBeDisabled()
  })

  it('an enabled/active mismatch asks for a restart even when the answer omits the flag', async () => {
    read.mockResolvedValue(setting({ enabled: true, active: false, restart_required: false }))
    mount()
    expect(await screen.findByTestId('shared-crew-preview-restart')).toHaveTextContent('Restart the gateway to apply')
  })

  it('a matching stored choice and running state asks for nothing', async () => {
    read.mockResolvedValue(setting({ enabled: true, active: true }))
    mount()
    await toggle()
    expect(screen.queryByTestId('shared-crew-preview-restart')).toBeNull()
  })

  it('a refused restart shows an error notice and keeps the button', async () => {
    read.mockResolvedValue(setting({ enabled: true, restart_required: true }))
    restart.mockRejectedValue(new ApiError(500, 'no', '{}'))
    mount()
    fireEvent.click(await screen.findByTestId('shared-crew-preview-restart-button'))
    expect(await screen.findByTestId('shared-crew-preview-restart-error')).toHaveAttribute('role', 'alert')
    expect(screen.getByTestId('shared-crew-preview-restart-button')).not.toBeDisabled()
  })

  it('the environment override shows the switch on and disabled, with a state label', async () => {
    read.mockResolvedValue(setting({ enabled: false, active: true, env_override: true }))
    mount()
    const sw = await toggle()
    expect(sw).toHaveAttribute('aria-checked', 'true')
    expect(sw).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByTestId('shared-crew-preview-env')).toHaveTextContent('On via environment')
    fireEvent.click(sw)
    expect(write).not.toHaveBeenCalled()
  })

  it('a failed write keeps the stored value and shows an error notice', async () => {
    read.mockResolvedValue(setting())
    write.mockRejectedValue(new ApiError(500, 'no', '{}'))
    mount()
    fireEvent.click(await toggle())
    expect(await screen.findByTestId('shared-crew-preview-save-error')).toHaveAttribute('role', 'alert')
    expect(screen.getByRole('switch', { name: 'Shared Crew' })).toHaveAttribute('aria-checked', 'false')
  })

  it.each([
    ['a gateway without the route', () => read.mockResolvedValue(null)],
    ['a caller who is not the owner', () => read.mockRejectedValue(new ApiError(403, 'owner only', '{"code":"owner_only"}'))],
  ])('draws no card for %s', async (_name, arrange) => {
    arrange()
    mount()
    await waitFor(() => expect(read).toHaveBeenCalled())
    await screen.findByRole('switch', { name: /Webhooks/i })
    expect(screen.queryByRole('switch', { name: 'Shared Crew' })).toBeNull()
    expect(screen.queryByTestId('shared-crew-preview-load-error')).toBeNull()
  })

  it('a failed read shows an error notice instead of a missing card', async () => {
    read.mockRejectedValue(new ApiError(500, 'boom', '{}'))
    mount()
    expect(await screen.findByTestId('shared-crew-preview-load-error')).toHaveAttribute('role', 'alert')
  })
})
