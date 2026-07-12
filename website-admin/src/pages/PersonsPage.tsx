import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { apiGet, apiPost } from '@/lib/api'
import type { PersonRow } from '@/lib/types'
import { StatusBadge } from '@/components/ui/Badge'
import { fmtDateShort, inr, cn } from '@/lib/utils'

const TABS = ['', 'ACTIVE', 'EXPIRED', 'UNDERPAID', 'CANCELLED', 'NONE'] as const
type Tab = typeof TABS[number]
const TAB_LABELS: Record<Tab, string> = { '': 'All', ACTIVE: 'Active', EXPIRED: 'Expired', UNDERPAID: 'Underpaid', CANCELLED: 'Cancelled', NONE: 'No Sub' }

export function PersonsPage() {
  const [tab, setTab] = useState<Tab>('')
  const navigate = useNavigate()
  const qc = useQueryClient()

  const qs = tab ? `?status=${tab}` : ''
  const { data: persons, isLoading, error } = useQuery({
    queryKey: ['persons', tab],
    queryFn: () => apiGet<PersonRow[]>(`/subscriptions/admin/persons${qs}`),
  })

  async function blockPerson(personId: number) {
    if (!confirm('Block this person?')) return
    try {
      await apiPost(`/subscriptions/admin/persons/${personId}/block`)
      qc.invalidateQueries({ queryKey: ['persons'] })
    } catch (e) { alert(`Failed: ${(e as Error).message}`) }
  }

  async function unblockPerson(personId: number) {
    if (!confirm('Unblock this person?')) return
    try {
      await apiPost(`/subscriptions/admin/persons/${personId}/unblock`)
      qc.invalidateQueries({ queryKey: ['persons'] })
    } catch (e) { alert(`Failed: ${(e as Error).message}`) }
  }

  return (
    <div>
      <h1 className="text-xl font-bold text-slate-900 mb-4">Persons</h1>

      <div className="flex border-b border-slate-200 mb-5 overflow-x-auto">
        {TABS.map(t => (
          <button key={t}
            onClick={() => setTab(t)}
            className={cn('px-4 py-2.5 text-sm font-medium border-b-2 -mb-px whitespace-nowrap transition-colors',
              tab === t
                ? 'border-blue-500 text-blue-600'
                : 'border-transparent text-slate-500 hover:text-slate-700'
            )}
          >
            {TAB_LABELS[t]}
          </button>
        ))}
      </div>

      {isLoading && <div className="text-slate-400 text-center py-12">Loading…</div>}
      {error && <div className="text-red-500 text-center py-12">{(error as Error).message}</div>}
      {persons && (
        <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="bg-slate-50">
                <th className="px-4 py-3 text-left text-[0.72rem] font-bold text-slate-500 uppercase tracking-wide border-b border-slate-200">Name</th>
                <th className="px-4 py-3 text-left text-[0.72rem] font-bold text-slate-500 uppercase tracking-wide border-b border-slate-200">User</th>
                <th className="px-4 py-3 text-left text-[0.72rem] font-bold text-slate-500 uppercase tracking-wide border-b border-slate-200">Status</th>
                <th className="px-4 py-3 text-left text-[0.72rem] font-bold text-slate-500 uppercase tracking-wide border-b border-slate-200">Expires</th>
                <th className="px-4 py-3 text-left text-[0.72rem] font-bold text-slate-500 uppercase tracking-wide border-b border-slate-200">Paid</th>
                <th className="px-4 py-3 text-left text-[0.72rem] font-bold text-slate-500 uppercase tracking-wide border-b border-slate-200">Required</th>
                <th className="px-4 py-3 border-b border-slate-200"></th>
              </tr>
            </thead>
            <tbody>
              {persons.length === 0 && (
                <tr><td colSpan={7} className="text-center text-slate-400 py-10">No persons found.</td></tr>
              )}
              {persons.map(p => {
                const st = (p.status || 'NONE').toUpperCase()
                return (
                  <tr key={p.person_id} className="border-b border-slate-100 last:border-0 hover:bg-slate-50">
                    <td className="px-4 py-3 font-medium text-slate-900">{p.display_name}</td>
                    <td className="px-4 py-3">
                      <button onClick={() => navigate(`/users/${p.user_id}`)} className="text-blue-600 hover:underline text-xs">
                        {p.user_email}
                      </button>
                    </td>
                    <td className="px-4 py-3"><StatusBadge status={p.status} /></td>
                    <td className="px-4 py-3 text-slate-600">{fmtDateShort(p.expires_at)}</td>
                    <td className="px-4 py-3 text-slate-600">{inr(p.paid_price)}</td>
                    <td className="px-4 py-3 text-slate-600">{inr(p.required_price)}</td>
                    <td className="px-4 py-3 text-right">
                      {st !== 'CANCELLED' ? (
                        <button onClick={() => blockPerson(p.person_id)}
                          className="text-xs bg-red-50 text-red-600 border border-red-200 rounded-lg px-3 py-1.5 hover:bg-red-100">
                          Block
                        </button>
                      ) : (
                        <button onClick={() => unblockPerson(p.person_id)}
                          className="text-xs bg-green-50 text-green-700 border border-green-200 rounded-lg px-3 py-1.5 hover:bg-green-100">
                          Unblock
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
