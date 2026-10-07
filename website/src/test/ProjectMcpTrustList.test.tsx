import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* ── Mock api/client BEFORE the component imports ── */
const mockApi = vi.hoisted(() => ({
  mcpProjectTrust: vi.fn(),
  revokeMcpProjectTrust: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import ProjectMcpTrustList from '../components/ProjectMcpTrustList'

function Harness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <ProjectMcpTrustList />
    </QueryClientProvider>
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.revokeMcpProjectTrust.mockResolvedValue({})
})

describe('ProjectMcpTrustList', () => {
  it('renders nothing when no folder has been trusted', async () => {
    mockApi.mcpProjectTrust.mockResolvedValue({ grants: [] })
    const { container } = render(<Harness />)
    await waitFor(() => expect(mockApi.mcpProjectTrust).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when the endpoint is unavailable', async () => {
    mockApi.mcpProjectTrust.mockResolvedValue(undefined)
    const { container } = render(<Harness />)
    await waitFor(() => expect(mockApi.mcpProjectTrust).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })

  it('shows an error, not an empty list, when the read fails', async () => {
    mockApi.mcpProjectTrust.mockRejectedValue(new Error('boom'))
    render(<Harness />)
    expect(await screen.findByText(/Couldn't load the projects trusted/)).toBeInTheDocument()
  })

  it('lists each folder, a missing one included', async () => {
    mockApi.mcpProjectTrust.mockResolvedValue({
      grants: [
        { path: '/home/user/repo-a', exists: true },
        { path: '/home/user/gone', exists: false },
      ],
    })
    render(<Harness />)
    expect(await screen.findByText('/home/user/repo-a')).toBeInTheDocument()
    expect(screen.getByText('/home/user/gone')).toBeInTheDocument()
    expect(screen.getByText(/no longer exists/)).toBeInTheDocument()
  })

  it('withdraws a folder in one click', async () => {
    mockApi.mcpProjectTrust.mockResolvedValue({ grants: [{ path: '/home/user/repo-a', exists: true }] })
    render(<Harness />)
    fireEvent.click(await screen.findByRole('button', { name: 'Withdraw' }))
    await waitFor(() => expect(mockApi.revokeMcpProjectTrust).toHaveBeenCalledWith('/home/user/repo-a'))
  })

  it('shows an error when the withdraw fails', async () => {
    mockApi.mcpProjectTrust.mockResolvedValue({ grants: [{ path: '/home/user/repo-a', exists: true }] })
    mockApi.revokeMcpProjectTrust.mockRejectedValue(new Error('boom'))
    render(<Harness />)
    fireEvent.click(await screen.findByRole('button', { name: 'Withdraw' }))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
  })
})
