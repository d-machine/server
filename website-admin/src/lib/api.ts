import { clearAdminCreds, getAuthHeader } from './auth'

const BASE_URL = import.meta.env.VITE_API_URL as string

function headers(extra: Record<string, string> = {}): Record<string, string> {
  return { Authorization: getAuthHeader(), ...extra }
}

async function handleRes<T>(res: Response): Promise<T> {
  if (res.status === 401) {
    clearAdminCreds()
    window.location.href = '/login'
    throw new Error('Unauthorized')
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({})) as { detail?: string }
    throw new Error(body.detail || `HTTP ${res.status}`)
  }
  return res.json() as Promise<T>
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, { headers: headers() })
  return handleRes<T>(res)
}

export async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'POST',
    headers: headers({ 'Content-Type': 'application/json' }),
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  return handleRes<T>(res)
}

export async function apiPatch<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'PATCH',
    headers: headers({ 'Content-Type': 'application/json' }),
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  return handleRes<T>(res)
}

export async function apiFormPost<T>(path: string, form: FormData): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'POST',
    headers: { Authorization: getAuthHeader() },
    body: form,
  })
  return handleRes<T>(res)
}
