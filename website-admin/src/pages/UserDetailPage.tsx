import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { apiGet, apiPost } from '@/lib/api'
import type { UserDetail } from '@/lib/types'
import { StatusBadge } from '@/components/ui/Badge'
import { fmtDateShort, inr } from '@/lib/utils'

export function UserDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()

  const { data: user, isLoading, error } = useQuery({
    queryKey: ['user', id],
    queryFn: () => apiGet<UserDetail>(`/subscriptions/admin/users/${id}`),
  })

  async function blockPerson(personId: number) {
    if (!confirm('Block this person? Their subscription will be set to CANCELLED.')) return
    try {
      await apiPost(`/subscriptions/admin/persons/${personId}/block`)
      qc.invalidateQueries({ queryKey: ['user', id] })
    } catch (e) {
      alert(`Failed: ${(e as Error).message}`)
    }
  }

  async function unblockPerson(personId: number) {
    if (!confirm('Unblock this person? Their subscription will be set to ACTIVE.')) return
    try {
      await apiPost(`/subscriptions/admin/persons/${personId}/unblock`)
      qc.invalidateQueries({ queryKey: ['user', id] })
    } catch (e) {
      alert(`Failed: ${(e as Error).message}`)
    }
  }

  if (isLoading) return <div className="text-slate-400 text-center py-16">Loading…</div>
  if (error) return <div className="text-red-500 text-center py-16">{(error as Error).message}</div>
  if (!user) return null

  return (
    <div>
      <div className="flex items-center gap-3 mb-5">
        <button onClick={() => navigate(-1)} className="text-sm text-slate-600 border border-slate-200 rounded-lg px-3 py-1.5 hover:bg-slate-100">
          ← Back
        </button>
        <h1 className="text-lg font-bold text-slate-900">User Detail</h1>
      </div>

      <div className="bg-white border border-slate-200 rounded-xl p-5 mb-4">
        <div className="text-base font-bold text-slate-900">{user.email}</div>
        <div className="text-sm text-slate-500 mt-1">
          {user.name || '(no name)'} · Registered: {fmtDateShort(user.created_at)}
        </div>
      </div>

      <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
        <table className="w-full text-sm border-collapse">
          <thead>
            <tr className="bg-slate-50">
              <th className="px-4 py-3 text-left text-[0.72rem] font-bold text-slate-500 uppercase tracking-wide border-b border-slate-200">Person</th>
              <th className="px-4 py-3 text-left text-[0.72rem] font-bold text-slate-500 uppercase tracking-wide border-b border-slate-200">Status</th>
              <th className="px-4 py-3 text-left text-[0.72rem] font-bold text-slate-500 uppercase tracking-wide border-b border-slate-200">Expires</th>
              <th className="px-4 py-3 text-left text-[0.72rem] font-bold text-slate-500 uppercase tracking-wide border-b border-slate-200">Paid</th>
              <th className="px-4 py-3 text-left text-[0.72rem] font-bold text-slate-500 uppercase tracking-wide border-b border-slate-200">Required</th>
              <th className="px-4 py-3 border-b border-slate-200"></th>
            </tr>
          </thead>
          <tbody>
            {user.persons.length === 0 && (
              <tr><td colSpan={6} className="text-center text-slate-400 py-10">No persons registered</td></tr>
            )}
            {user.persons.map(p => {
              const st = (p.status || 'NONE').toUpperCase()
              return (
                <tr key={p.person_id} className="border-b border-slate-100 last:border-0 hover:bg-slate-50">
                  <td className="px-4 py-3 font-medium text-slate-900">{p.display_name}</td>
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
    </div>
  )
}
