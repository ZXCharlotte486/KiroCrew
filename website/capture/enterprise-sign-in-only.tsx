/**
 * Isolated capture entry for the enterprise sign-in-only screen.
 *
 * Mounts the REAL KiroPrerequisiteGate against the real stylesheet, theme tokens
 * and i18n catalog, with the prerequisite status a signed-out enterprise gateway's
 * sign-in-only app answers (`enterprise_sign_in_only: true`). Only the API calls
 * are stubbed.
 *
 * Scene from the query string: ?scene=signed-out|signed-in&theme=dark|light&lang=en
 *   signed-out: the Company SSO form the user signs in with.
 *   signed-in:  the wait while the gateway loads the rules and starts.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import KiroPrerequisiteGate from '../src/components/KiroPrerequisiteGate'
import { api } from '../src/api/client'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') || 'signed-out'
const lang = params.get('lang') || 'en'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n(lang)

// What the sign-in-only app's synthetic GET /api/kiro-prerequisite answers. The
// `stopped` scene answers once, then fails as a gateway that exited does, with
// the clock moved past the hand-off window.
let answered = false
let skew = 0
const realNow = Date.now.bind(Date)
api.kiroPrerequisite = () => {
  if (scene === 'stopped' && answered) {
    // Each failed poll moves the clock past the hand-off window once more.
    skew += 130_000
    Date.now = () => realNow() + skew
    return Promise.reject(new Error('Failed to fetch')) as ReturnType<typeof api.kiroPrerequisite>
  }
  answered = true
  return Promise.resolve({
    ready: false,
    authenticated: false,
    installed: true,
    initial_setup_complete: false,
    setup_allowed: true,
    enterprise_login_required: true,
    enterprise_sign_in_only: true,
  }) as unknown as ReturnType<typeof api.kiroPrerequisite>
}
api.kasLoginStatus = () =>
  Promise.resolve(
    scene !== 'signed-out'
      ? { authenticated: true, provider: 'idc', identity: 'identity_center', transport: 'device' }
      : { authenticated: false, provider: null, identity: null, transport: 'device' },
  ) as unknown as ReturnType<typeof api.kasLoginStatus>
api.kasLoginPoll = () =>
  Promise.resolve({ status: 'pending' }) as ReturnType<typeof api.kasLoginPoll>

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <KiroPrerequisiteGate>
      <div>dashboard</div>
    </KiroPrerequisiteGate>
  </QueryClientProvider>,
)
