export interface AdminCreds {
  username: string
  password: string
}

const KEY = 'admin_creds'

export function getAdminCreds(): AdminCreds | null {
  try {
    const raw = sessionStorage.getItem(KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

export function saveAdminCreds(creds: AdminCreds): void {
  sessionStorage.setItem(KEY, JSON.stringify(creds))
}

export function clearAdminCreds(): void {
  sessionStorage.removeItem(KEY)
}

export function getAuthHeader(): string {
  const c = getAdminCreds()
  if (!c) return ''
  return `Basic ${btoa(`${c.username}:${c.password}`)}`
}
