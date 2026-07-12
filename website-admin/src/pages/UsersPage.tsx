import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import type { UserSummary } from '@/lib/types'
import { inr } from '@/lib/utils'

export function UsersPage() {
  const navigate = useNavigate()
  const { data: users, isLoading, error } = useQuery({
    queryKey: ['users'],
    queryFn: () => apiGet<UserSummary[]>('/subscriptions/admin/users'),
  })

  return (
    <div>
      <h1 className="text-xl font-bold text-slate-900 mb-5">Users</h1>
      {isLoading && <div className="text-slate-400 text-center py-12">Loading…</div>}
      {error && <div className="text-red-500 text-center py-12">{(error as Error).message}</div>}
      {users && (
        <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="bg-slate-50">
                <th className="px-4 py-3 text-left text-[0.72rem] font-bold text-slate-500 uppercase tracking-wide border-b border-slate-200">Email</th>
                <th className="px-4 py-3 text-left text-[0.72rem] font-bold text-slate-500 uppercase tracking-wide border-b border-slate-200">Name</th>
                <th className="px-4 py-3 text-left text-[0.72rem] font-bold text-slate-500 uppercase tracking-wide border-b border-slate-200">Persons</th>
                <th className="px-4 py-3 text-left text-[0.72rem] font-bold text-slate-500 uppercase tracking-wide border-b border-slate-200">Total Paid</th>
                <th className="px-4 py-3 border-b border-slate-200"></th>
              </tr>
            </thead>
            <tbody>
              {users.length === 0 && (
                <tr><td colSpan={5} className="text-center text-slate-400 py-10">No users found.</td></tr>
              )}
              {users.map(u => (
                <tr key={u.user_id} className="hover:bg-slate-50 border-b border-slate-100 last:border-0">
                  <td className="px-4 py-3 font-medium text-slate-900">{u.email}</td>
                  <td className="px-4 py-3 text-slate-600">{u.name || '—'}</td>
                  <td className="px-4 py-3 text-slate-600">{u.person_count}</td>
                  <td className="px-4 py-3 text-slate-600">{inr(u.total_paid)}</td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => navigate(`/users/${u.user_id}`)}
                      className="text-sm border border-slate-200 rounded-lg px-3 py-1.5 hover:bg-slate-100 transition-colors"
                    >
                      View →
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
