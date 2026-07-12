import { Routes, Route, Navigate } from 'react-router-dom'
import { AppShell } from '@/components/layout/AppShell'
import { LoginPage }        from '@/pages/LoginPage'
import { DashboardPage }    from '@/pages/DashboardPage'
import { TicketsPage }      from '@/pages/TicketsPage'
import { TicketDetailPage } from '@/pages/TicketDetailPage'
import { UsersPage }        from '@/pages/UsersPage'
import { UserDetailPage }   from '@/pages/UserDetailPage'
import { PersonsPage }      from '@/pages/PersonsPage'
import { CreateTicketPage } from '@/pages/CreateTicketPage'

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<AppShell />}>
        <Route path="/"                   element={<DashboardPage />} />
        <Route path="/tickets"            element={<TicketsPage />} />
        <Route path="/tickets/create"     element={<CreateTicketPage />} />
        <Route path="/tickets/:id"        element={<TicketDetailPage />} />
        <Route path="/users"              element={<UsersPage />} />
        <Route path="/users/:id"          element={<UserDetailPage />} />
        <Route path="/persons"            element={<PersonsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
