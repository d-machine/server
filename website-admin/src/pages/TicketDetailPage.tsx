import { useState, useRef } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { apiGet, apiPost, apiPatch, apiFormPost } from '@/lib/api'
import type { Ticket } from '@/lib/types'
import { StatusBadge } from '@/components/ui/Badge'
import { fmtDate, inr } from '@/lib/utils'

export function TicketDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [declining, setDeclining] = useState(false)
  const [declineReason, setDeclineReason] = useState('')
  const [actionError, setActionError] = useState('')
  const amountRefs = useRef<Record<number, HTMLInputElement | null>>({})
  const notesRefs  = useRef<Record<number, HTMLInputElement | null>>({})

  const { data: ticket, isLoading, error } = useQuery({
    queryKey: ['ticket', id],
    queryFn: () => apiGet<Ticket>(`/tickets/admin/${id}`),
  })

  async function approve() {
    if (!ticket) return
    setActionError('')
    try {
      for (const p of ticket.persons) {
        const amt   = amountRefs.current[p.id]?.value
        const notes = notesRefs.current[p.id]?.value
        if (amt !== undefined || notes) {
          await apiPatch(`/tickets/admin/${id}/persons/${p.id}`, {
            approved_amount: amt ? parseInt(amt) : null,
            notes: notes?.trim() || null,
          })
        }
      }
      await apiPost(`/tickets/admin/${id}/approve`)
      qc.invalidateQueries({ queryKey: ['ticket', id] })
      qc.invalidateQueries({ queryKey: ['tickets'] })
    } catch (e) {
      setActionError((e as Error).message)
    }
  }

  async function submitDecline() {
    if (!declineReason.trim()) return
    setActionError('')
    const fd = new FormData()
    fd.append('reason', declineReason.trim())
    try {
      await apiFormPost(`/tickets/admin/${id}/decline`, fd)
      setDeclining(false)
      qc.invalidateQueries({ queryKey: ['ticket', id] })
      qc.invalidateQueries({ queryKey: ['tickets'] })
    } catch (e) {
      setActionError((e as Error).message)
    }
  }

  if (isLoading) return <div className="text-slate-400 text-center py-16">Loading…</div>
  if (error) return <div className="text-red-500 text-center py-16">{(error as Error).message}</div>
  if (!ticket) return null

  const isPending = ticket.status === 'PENDING'

  return (
    <div>
      <div className="flex items-center gap-3 mb-5">
        <button onClick={() => navigate(-1)} className="text-sm text-slate-600 border border-slate-200 rounded-lg px-3 py-1.5 hover:bg-slate-100">
          ← Back
        </button>
        <h1 className="text-lg font-bold text-slate-900">Ticket #{ticket.ticket_id}</h1>
        <StatusBadge status={ticket.status} />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-[1fr_280px] gap-4">
        {/* Left column */}
        <div className="space-y-4">
          {/* User info */}
          <div className="bg-white border border-slate-200 rounded-xl p-5">
            <p className="text-xs font-bold text-slate-500 uppercase tracking-wide mb-3">User</p>
            <div className="text-base font-semibold text-slate-900">{ticket.user_name || '(no name)'}</div>
            <button
              onClick={() => navigate(`/users/${ticket.user_id}`)}
              className="text-sm text-blue-600 hover:underline mt-0.5 block"
            >
              {ticket.user_email}
            </button>
            <div className="text-xs text-slate-400 mt-2">
              Submitted: {fmtDate(ticket.submitted_at)}
              {ticket.resolved_at && ` · Resolved: ${fmtDate(ticket.resolved_at)}`}
            </div>
          </div>

          {/* Persons & amounts */}
          <div className="bg-white border border-slate-200 rounded-xl p-5">
            <p className="text-xs font-bold text-slate-500 uppercase tracking-wide mb-4">Persons &amp; Amounts</p>
            <div className="space-y-0">
              {ticket.persons.map(p => (
                <div key={p.id} className="flex items-center gap-3 py-3 border-b border-slate-50 last:border-0">
                  <div className="font-medium text-slate-900 text-sm flex-1">{p.display_name}</div>
                  <div className="text-right text-sm text-slate-500 w-20">
                    Submitted<br />
                    <strong className="text-slate-900">{inr(p.amount)}</strong>
                  </div>
                  {isPending ? (
                    <>
                      <div>
                        <div className="text-[0.7rem] text-slate-500 mb-1">Approved amt</div>
                        <input
                          type="number"
                          ref={el => amountRefs.current[p.id] = el}
                          defaultValue={p.approved_amount ?? p.amount}
                          className="w-28 border border-slate-300 rounded-lg px-2 py-1.5 text-sm text-right focus:outline-none focus:ring-2 focus:ring-blue-500"
                        />
                      </div>
                      <div className="flex-1">
                        <div className="text-[0.7rem] text-slate-500 mb-1">Notes</div>
                        <input
                          type="text"
                          ref={el => notesRefs.current[p.id] = el}
                          defaultValue={p.notes || ''}
                          placeholder="Optional note"
                          className="w-full border border-slate-300 rounded-lg px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                        />
                      </div>
                    </>
                  ) : (
                    <>
                      <div className="text-right text-sm text-slate-500 w-20">
                        Approved<br />
                        <strong className="text-green-700">{inr(p.approved_amount ?? p.amount)}</strong>
                      </div>
                      <div className="flex-1 text-sm text-slate-500">{p.notes || ''}</div>
                    </>
                  )}
                </div>
              ))}
            </div>

            {ticket.decline_reason && (
              <div className="mt-4 bg-red-50 border border-red-200 rounded-xl p-4 text-sm text-red-700">
                <strong>Decline reason:</strong> {ticket.decline_reason}
              </div>
            )}
          </div>

          {/* Action buttons */}
          {isPending && (
            <div className="flex gap-3">
              <button
                onClick={approve}
                className="bg-green-50 text-green-700 border border-green-200 rounded-xl px-6 py-2.5 text-sm font-medium hover:bg-green-100 transition-colors"
              >
                ✓ Approve
              </button>
              <button
                onClick={() => { setDeclining(true); setDeclineReason(''); setActionError('') }}
                className="bg-red-50 text-red-600 border border-red-200 rounded-xl px-6 py-2.5 text-sm font-medium hover:bg-red-100 transition-colors"
              >
                ✕ Decline
              </button>
            </div>
          )}
          {actionError && <p className="text-sm text-red-600">{actionError}</p>}
        </div>

        {/* Right column: screenshot */}
        <div className="bg-white border border-slate-200 rounded-xl p-5">
          <p className="text-xs font-bold text-slate-500 uppercase tracking-wide mb-3">Screenshot</p>
          {ticket.screenshot_url ? (
            <div>
              <a href={ticket.screenshot_url} target="_blank" rel="noreferrer">
                <img
                  src={ticket.screenshot_url}
                  alt="Payment screenshot"
                  className="max-w-full max-h-80 object-contain border border-slate-200 rounded-xl"
                />
              </a>
              <a href={ticket.screenshot_url} target="_blank" rel="noreferrer"
                className="block mt-2 text-xs text-center text-blue-600 hover:underline">
                Open full size ↗
              </a>
            </div>
          ) : (
            <div className="border border-slate-200 rounded-xl p-8 text-center text-slate-400 text-sm">
              No screenshot uploaded
            </div>
          )}
        </div>
      </div>

      {/* Decline modal */}
      {declining && (
        <div className="fixed inset-0 bg-slate-900/60 flex items-center justify-center z-50">
          <div className="bg-white rounded-2xl shadow-2xl p-7 w-full max-w-md mx-4">
            <h2 className="text-base font-bold text-slate-900 mb-2">Decline Ticket</h2>
            <p className="text-sm text-slate-500 mb-4">Provide a reason so the user knows what to fix:</p>
            <textarea
              value={declineReason}
              onChange={e => setDeclineReason(e.target.value)}
              rows={4}
              placeholder="e.g. Screenshot is blurry, amount doesn't match…"
              className="w-full border border-slate-300 rounded-xl px-3 py-2.5 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            {actionError && <p className="text-sm text-red-600 mt-2">{actionError}</p>}
            <div className="flex gap-3 mt-4">
              <button onClick={() => setDeclining(false)} className="flex-1 border border-slate-200 rounded-xl py-2.5 text-sm font-medium hover:bg-slate-50">
                Cancel
              </button>
              <button onClick={submitDecline} className="flex-1 bg-red-50 text-red-600 border border-red-200 rounded-xl py-2.5 text-sm font-medium hover:bg-red-100">
                Decline Ticket
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
