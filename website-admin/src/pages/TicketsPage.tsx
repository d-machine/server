import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import type { Ticket } from '@/lib/types'
import { StatusBadge } from '@/components/ui/Badge'
import { fmtDate, inr } from '@/lib/utils'
import { cn } from '@/lib/utils'

const TABS = ['ALL', 'APPROVED', 'DECLINED'] as const
type Tab = typeof TABS[number]

export function TicketsPage() {
  const [tab, setTab] = useState<Tab>('ALL')
  const navigate = useNavigate()

  const qs = tab !== 'ALL' ? `?status=${tab}` : ''
  const { data, isLoading, error } = useQuery({
    queryKey: ['tickets', tab],
    queryFn: () => apiGet<Ticket[]>(`/tickets/admin${qs}`),
  })

  const tickets = tab === 'ALL' ? (data ?? []).filter(t => t.status !== 'PENDING') : (data ?? [])

  return (
    <div>
      <h1 className="text-xl font-bold text-slate-900 mb-4">Past Tickets</h1>

      <div className="flex border-b border-slate-200 mb-5 overflow-x-auto">
        {TABS.map(t => (
          <button key={t}
            onClick={() => setTab(t)}
            className={cn('px-5 py-2.5 text-sm font-medium border-b-2 -mb-px whitespace-nowrap transition-colors',
              tab === t
                ? 'border-blue-500 text-blue-600'
                : 'border-transparent text-slate-500 hover:text-slate-700'
            )}
          >
            {t === 'ALL' ? 'All' : t.charAt(0) + t.slice(1).toLowerCase()}
          </button>
        ))}
      </div>

      {isLoading && <div className="text-slate-400 text-center py-12">Loading…</div>}
      {error && <div className="text-red-500 text-center py-12">{(error as Error).message}</div>}
      {!isLoading && tickets.length === 0 && <div className="text-slate-400 text-center py-12">No tickets found.</div>}
      {tickets.map(t => (
        <div key={t.ticket_id}
          onClick={() => navigate(`/tickets/${t.ticket_id}`)}
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
              <div className="text-xl font-bold text-slate-900">
                {inr(t.persons.reduce((s, p) => s + (p.amount || 0), 0))}
              </div>
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}
