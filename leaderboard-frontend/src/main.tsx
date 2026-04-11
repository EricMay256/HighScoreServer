import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import './styles.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Data is considered fresh for 30 seconds. Within that window a
      // component re-mounting or the same query key rendering elsewhere
      // will read from cache rather than firing a new request.
      staleTime: 30_000,
      // Don't re-fetch just because the user switched browser tabs and came
      // back — the leaderboard data isn't real-time sensitive enough to
      // warrant the extra traffic.
      refetchOnWindowFocus: false,
    },
  },
})

const container = document.getElementById('root')
if (!container) throw new Error('Root element #root not found in index.html')

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
)
