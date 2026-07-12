import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import type { Ticket } from '@/lib/types'
import { StatusBadge } from '@/components/ui/Badge'
import { fmtDate, inr } from '@/lib/utils'

export function DashboardPage() {
  const navigate = useNavigate()
  const { data: tickets, isLoading, error } = useQuery({
    queryKey: ['tickets', 'PENDING'],
    queryFn: () => apiGet<Ticket[]>('/tickets/admin?status=PENDING'),
  })

  return (
    <div>
      <div className="flex items-center justify-between mb-5">
        <h1 className="text-xl font-bold text-slate-900">Pending Tickets</h1>
        <button
          onClick={() => navigate('/tickets/create')}
          className="bg-blue-600 text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors"
        >
          + Create Ticket
        </button>
      </div>

      {isLoading && <div className="text-slate-400 text-sm text-center py-12">Loading…</div>}
      {error && <div className="text-red-500 text-sm text-center py-12">{(error as Error).message}</div>}
      {tickets && tickets.length === 0 && (
        <div className="text-center py-12 text-slate-400">No pending tickets — all caught up! 🎉</div>
      )}
      {tickets && tickets.map(t => <TicketCard key={t.ticket_id} ticket={t} onClick={() => navigate(`/tickets/${t.ticket_id}`)} />)}
    </div>
  )
}

function TicketCard({ ticket: t, onClick }: { ticket: Ticket; onClick: () => void }) {
  const total = t.persons.reduce((s, p) => s + (p.amount || 0), 0)
  return (
    <div
      onClick={onClick}
      className="bg-white border border-slate-200 rounded-xl p-5 mb-3 cursor-pointer hover:shadow-md hover:border-blue-200 transition-all"
    >
      <div className="flex items-start gap-4 justify-between">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1.5">
            <StatusBadge status={t.status} />
            <span className="text-xs text-slate-400">#{t.ticket_id} · {fmtDate(t.submitted_at)}</span>
          </div>
          <div>
            <span className="font-semibold text-slate-900">{t.user_name || ''}</span>
            <span className="text-slate-500 text-sm"> &lt;{t.user_email}&gt;</span>
          </div>
          <div className="flex flex-wrap gap-1 mt-2">
            {t.persons.map(p => (
              <span key={p.id} className="bg-slate-100 text-slate-600 text-xs px-2 py-0.5 rounded-full">
                {p.display_name} · {inr(p.amount)}
              </span>
            ))}
          </div>
        </div>
        <div className="text-right shrink-0">
          <div className="text-xl font-bold text-slate-900">{inr(total)}</div>
        </div>
      </div>
    </div>
  )
}
