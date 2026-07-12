export interface TicketPerson {
  id: number
  person_id: number
  display_name: string
  amount: number
  approved_amount: number | null
  notes: string | null
}

export interface Ticket {
  ticket_id: number
  user_id: number
  user_name: string | null
  user_email: string
  status: string
  submitted_at: string
  resolved_at: string | null
  screenshot_url: string | null
  decline_reason: string | null
  persons: TicketPerson[]
}

export interface UserSummary {
  user_id: number
  email: string
  name: string | null
  person_count: number
  total_paid: number | null
}

export interface PersonSub {
  person_id: number
  display_name: string
  status: string | null
  expires_at: string | null
  paid_price: number | null
  required_price: number | null
}

export interface UserDetail {
  user_id: number
  email: string
  name: string | null
  created_at: string
  persons: (PersonSub & { user_id: number; user_email: string })[]
}

export interface PersonRow extends PersonSub {
  user_id: number
  user_email: string
}
