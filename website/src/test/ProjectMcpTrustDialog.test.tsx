import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* ── Mock api/client BEFORE the component imports ── */
const mockApi = vi.hoisted(() => ({
  grantMcpProjectTrust: vi.fn(),
  mcpProjectTrust: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import ProjectMcpTrustDialog from '../components/ProjectMcpTrustDialog'
import ProjectMcpTrustNotice from '../components/ProjectMcpTrustNotice'

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>
}

async function enabledTrustButton() {
  const btn = await screen.findByRole('button', { name: /Trust and run/ })
  await waitFor(() => expect(btn).not.toBeDisabled())
  return btn
}

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.grantMcpProjectTrust.mockResolvedValue({ trusted: true })
  mockApi.mcpProjectTrust.mockResolvedValue({
    project: '/work/checkout-service',
    project_key: '/canonical/checkout-service',
    trusted: false,
    servers: [
      { agent: 'kirocrew', name: 'repo-db', command: '/opt/db', args: ['-v', 'x'], env_keys: ['DB_TOKEN'] },
      { agent: 'kirocrew', name: 'repo-lint', command: '', args: [], url: 'https://lint.invalid/mcp' },
    ],
    launch: { kirocrew: 'a'.repeat(64) },
  })
})

describe('ProjectMcpTrustDialog', () => {
  it('renders nothing when closed', () => {
    render(wrap(<ProjectMcpTrustDialog open={false} slotKey="dashboard:chat-7" onClose={vi.fn()} />))
    expect(screen.queryByText(/Run this project's MCP servers and hooks\?/)).not.toBeInTheDocument()
  })

  it('names the folder and the servers it would start', async () => {
    render(wrap(<ProjectMcpTrustDialog open slotKey="dashboard:chat-7" onClose={vi.fn()} />))
    expect(await screen.findByText('/work/checkout-service')).toBeInTheDocument()
    expect(await screen.findByText('repo-db')).toBeInTheDocument()
    expect(screen.getByText('/opt/db -v x')).toBeInTheDocument()
    expect(screen.getByText('https://lint.invalid/mcp')).toBeInTheDocument()
    // Env names are shown, never values.
    expect(screen.getByText(/values hidden\): DB_TOKEN/)).toBeInTheDocument()
  })

  it('says plainly the servers run as you, outside the tool sandbox', async () => {
    render(wrap(<ProjectMcpTrustDialog open slotKey="dashboard:chat-7" onClose={vi.fn()} />))
    expect(await screen.findByText(/run as you, outside the tool sandbox/)).toBeInTheDocument()
    expect(screen.getByText(/separate from trusting the project's skills/)).toBeInTheDocument()
  })

  it('grants with the reviewed key for the requesting slot', async () => {
    const onTrusted = vi.fn()
    const onClose = vi.fn()
    render(wrap(
      <ProjectMcpTrustDialog open slotKey="dashboard:chat-7" onClose={onClose} onTrusted={onTrusted} />,
    ))
    fireEvent.click(await enabledTrustButton())
    await waitFor(() => expect(onTrusted).toHaveBeenCalled())
    expect(mockApi.grantMcpProjectTrust).toHaveBeenCalledWith(
      'dashboard:chat-7', '/canonical/checkout-service', { kirocrew: 'a'.repeat(64) },
    )
    expect(onClose).toHaveBeenCalled()
  })

  it('reports failure when the response is not trusted', async () => {
    mockApi.grantMcpProjectTrust.mockResolvedValue({ trusted: false })
    const onTrusted = vi.fn()
    render(wrap(<ProjectMcpTrustDialog open slotKey="dashboard:chat-7" onClose={vi.fn()} onTrusted={onTrusted} />))
    fireEvent.click(await enabledTrustButton())
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(onTrusted).not.toHaveBeenCalled()
  })

  it('shows an error when the trust state cannot be read', async () => {
    mockApi.mcpProjectTrust.mockRejectedValue(new Error('boom'))
    render(wrap(<ProjectMcpTrustDialog open slotKey="dashboard:chat-7" onClose={vi.fn()} />))
    expect(await screen.findByText(/Couldn't read this project's MCP trust state/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Trust and run/ })).toBeDisabled()
  })

  it('cannot consent before the folder is known', async () => {
    mockApi.mcpProjectTrust.mockResolvedValue({ servers: [] })
    render(wrap(<ProjectMcpTrustDialog open slotKey="dashboard:chat-7" onClose={vi.fn()} />))
    const btn = await screen.findByRole('button', { name: /Trust and run/ })
    await waitFor(() => expect(mockApi.mcpProjectTrust).toHaveBeenCalled())
    expect(btn).toBeDisabled()
  })
})

describe('ProjectMcpTrustDialog hooks', () => {
  it('lists hooks with an off hook marked', async () => {
    mockApi.mcpProjectTrust.mockResolvedValue({
      project: '/p', project_key: '/p', trusted: false, servers: [],
      hooks: [
        { agent: 'a', event: 'preToolUse', command: './check.sh', matcher: 'execute_bash', enabled: true },
        { agent: 'a', event: 'agentSpawn', command: './off.sh', enabled: false },
      ],
      launch: { a: 'b'.repeat(64) },
    })
    render(wrap(<ProjectMcpTrustDialog open slotKey="dashboard:chat-7" onClose={vi.fn()} />))
    expect(await screen.findByText('./check.sh')).toBeInTheDocument()
    expect(screen.getByText(/agentSpawn \(off\)/)).toBeInTheDocument()
  })

  it('warns plainly that an agent can change the trusted code', async () => {
    render(wrap(<ProjectMcpTrustDialog open slotKey="dashboard:chat-7" onClose={vi.fn()} />))
    expect(await screen.findByText(/An agent working here can change that code/)).toBeInTheDocument()
  })

  it('refuses consent and explains when the project has too many specs', async () => {
    mockApi.mcpProjectTrust.mockResolvedValue({ project: '/p', project_key: '/p', trusted: false, too_many_specs: true, launch: {} })
    render(wrap(<ProjectMcpTrustDialog open slotKey="dashboard:chat-7" onClose={vi.fn()} />))
    expect(await screen.findByText(/more than 64 agent specs/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Trust and run/ })).toBeDisabled()
  })
})

describe('ProjectMcpTrustNotice', () => {
  it('shows a review button when the project has untrusted servers', async () => {
    render(wrap(<ProjectMcpTrustNotice slotKey="dashboard:chat-7" project="/work/checkout-service" />))
    expect(await screen.findByText(/declares 2 MCP servers and hooks/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Review' }))
    expect(await screen.findByText(/Run this project's MCP servers and hooks\?/)).toBeInTheDocument()
  })

  it('shows for a project that declares only hooks', async () => {
    mockApi.mcpProjectTrust.mockResolvedValue({
      trusted: false, servers: [], hooks: [{ agent: 'a', event: 'preToolUse', command: './x.sh' }],
    })
    render(wrap(<ProjectMcpTrustNotice slotKey="dashboard:chat-7" project="/work/checkout-service" />))
    expect(await screen.findByText(/declares 1 MCP server or hook/)).toBeInTheDocument()
  })

  it('shows an error, not nothing, when the project has too many specs', async () => {
    mockApi.mcpProjectTrust.mockResolvedValue({ trusted: false, servers: [], too_many_specs: true })
    render(wrap(<ProjectMcpTrustNotice slotKey="dashboard:chat-7" project="/work/checkout-service" />))
    expect(await screen.findByText(/more than 64 agent specs/)).toBeInTheDocument()
  })

  it('renders nothing once the project is trusted', async () => {
    mockApi.mcpProjectTrust.mockResolvedValue({
      trusted: true,
      servers: [{ agent: 'kirocrew', name: 'repo-db', command: '/opt/db', args: [] }],
    })
    const { container } = render(wrap(
      <ProjectMcpTrustNotice slotKey="dashboard:chat-7" project="/work/checkout-service" />,
    ))
    await waitFor(() => expect(mockApi.mcpProjectTrust).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })

  it('shows an error when the read fails for another reason', async () => {
    mockApi.mcpProjectTrust.mockRejectedValue(Object.assign(new Error('boom'), { status: 500, body: '' }))
    render(wrap(<ProjectMcpTrustNotice slotKey="dashboard:chat-7" project="/work/checkout-service" />))
    expect(await screen.findByText(/Couldn't check whether this project's MCP servers/)).toBeInTheDocument()
  })

  it('names omitted servers and refuses consent without a launch set', async () => {
    mockApi.mcpProjectTrust.mockResolvedValue({
      project: '/p', project_key: '/p', trusted: false,
      servers: [{ agent: 'a', name: 's', command: '/x', args: [] }], servers_omitted: 3,
    })
    render(wrap(<ProjectMcpTrustDialog open slotKey="dashboard:chat-7" onClose={vi.fn()} />))
    expect(await screen.findByText(/3 more servers are not shown/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Trust and run/ })).toBeDisabled()
  })

  it('renders nothing when the owner gate refuses the read', async () => {
    mockApi.mcpProjectTrust.mockRejectedValue(
      Object.assign(new Error('403'), { status: 403, body: '{"code":"owner_only"}' }),
    )
    const { container } = render(wrap(
      <ProjectMcpTrustNotice slotKey="dashboard:chat-7" project="/work/checkout-service" />,
    ))
    await waitFor(() => expect(mockApi.mcpProjectTrust).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })
})
