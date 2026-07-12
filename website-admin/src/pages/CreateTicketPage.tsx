import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { apiGet, apiFormPost } from '@/lib/api'
import type { UserSummary, UserDetail } from '@/lib/types'

export function CreateTicketPage() {
  const navigate = useNavigate()
  const [userId, setUserId] = useState('')
  const [selections, setSelections] = useState<Record<number, boolean>>({})
  const [amounts, setAmounts] = useState<Record<number, number>>({})
  const [screenshot, setScreenshot] = useState<File | null>(null)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const { data: users } = useQuery({
    queryKey: ['users'],
    queryFn: () => apiGet<UserSummary[]>('/subscriptions/admin/users'),
  })

  const { data: userDetail } = useQuery({
    queryKey: ['user', userId],
    queryFn: () => apiGet<UserDetail>(`/subscriptions/admin/users/${userId}`),
    enabled: !!userId,
  })

  async function submit() {
    if (!userId) { setError('Please select a user'); return }
    const persons = (userDetail?.persons ?? [])
      .filter(p => selections[p.person_id])
      .map(p => ({ person_id: p.person_id, amount: amounts[p.person_id] ?? 1000 }))
    if (!persons.length) { setError('Select at least one person'); return }
    setError('')
    setSubmitting(true)
    const fd = new FormData()
    fd.append('user_id', userId)
    fd.append('persons', JSON.stringify(persons))
    if (screenshot) fd.append('screenshot', screenshot)
    try {
      const result = await apiFormPost<{ ticket_id: number }>('/tickets/admin/create', fd)
      navigate(`/tickets/${result.ticket_id}`)
    } catch (e) {
      setError((e as Error).message)
      setSubmitting(false)
    }
  }

  return (
    <div>
      <div className="flex items-center gap-3 mb-5">
        <button onClick={() => navigate(-1)} className="text-sm text-slate-600 border border-slate-200 rounded-lg px-3 py-1.5 hover:bg-slate-100">
          ← Back
        </button>
        <h1 className="text-lg font-bold text-slate-900">Create Ticket on Behalf of User</h1>
      </div>

      <div className="bg-white border border-slate-200 rounded-xl p-6 max-w-xl space-y-4">
        <div>
          <label className="block text-xs font-bold text-slate-500 uppercase tracking-wide mb-1.5">Select User</label>
          <select
            value={userId}
            onChange={e => { setUserId(e.target.value); setSelections({}); setAmounts({}) }}
            className="w-full border border-slate-300 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            <option value="">— Select a user —</option>
            {users?.map(u => (
              <option key={u.user_id} value={u.user_id}>
                {u.email}{u.name ? ` (${u.name})` : ''}
              </option>
            ))}
          </select>
        </div>

        {userDetail && userDetail.persons.length > 0 && (
          <div>
            <label className="block text-xs font-bold text-slate-500 uppercase tracking-wide mb-2">Select Persons &amp; Amounts</label>
            <div className="space-y-0">
              {userDetail.persons.map(p => (
                <div key={p.person_id} className="flex items-center gap-3 py-2.5 border-b border-slate-100 last:border-0">
                  <input
                    type="checkbox"
                    id={`p-${p.person_id}`}
                    checked={!!selections[p.person_id]}
                    onChange={e => setSelections(prev => ({ ...prev, [p.person_id]: e.target.checked }))}
                    className="w-4 h-4 cursor-pointer"
                  />
                  <label htmlFor={`p-${p.person_id}`} className="flex-1 text-sm font-medium text-slate-900 cursor-pointer">
                    {p.display_name}
                  </label>
                  <div className="flex items-center gap-1">
                    <span className="text-slate-400 text-sm">₹</span>
                    <input
                      type="number"
                      value={amounts[p.person_id] ?? 1000}
                      onChange={e => setAmounts(prev => ({ ...prev, [p.person_id]: parseInt(e.target.value) || 1000 }))}
                      disabled={!selections[p.person_id]}
                      className="w-28 border border-slate-300 rounded-lg px-2 py-1.5 text-sm text-right disabled:bg-slate-50 disabled:text-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500"
                    />
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        <div>
          <label className="block text-xs font-bold text-slate-500 uppercase tracking-wide mb-1.5">
            Screenshot <span className="font-normal text-slate-400">(optional)</span>
          </label>
          <input
            type="file"
            accept="image/*"
            onChange={e => setScreenshot(e.target.files?.[0] ?? null)}
            className="text-sm text-slate-600"
          />
        </div>

        {error && <p className="text-sm text-red-600">{error}</p>}

        <div className="flex gap-3 pt-1">
          <button onClick={() => navigate(-1)} className="flex-1 border border-slate-200 rounded-xl py-2.5 text-sm font-medium hover:bg-slate-50">
            Cancel
          </button>
          <button onClick={submit} disabled={submitting}
            className="flex-1 bg-blue-600 text-white rounded-xl py-2.5 text-sm font-medium hover:bg-blue-700 disabled:opacity-50">
            {submitting ? 'Creating…' : 'Create Ticket'}
          </button>
        </div>
      </div>
    </div>
  )
}
