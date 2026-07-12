import { Navigate, Outlet } from 'react-router-dom'
import { getAdminCreds } from '@/lib/auth'
import { Navbar } from './Navbar'

export function AppShell() {
  const creds = getAdminCreds()
  if (!creds) return <Navigate to="/login" replace />
  return (
    <div className="min-h-screen bg-slate-50">
      <Navbar username={creds.username} />
      <main className="max-w-[1200px] mx-auto px-5 py-6">
        <Outlet />
      </main>
    </div>
  )
}
