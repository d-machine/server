import { cn } from '@/lib/utils'

const STATUS_STYLES: Record<string, string> = {
  PENDING:          'bg-yellow-100 text-yellow-800',
  APPROVED:         'bg-green-100 text-green-800',
  DECLINED:         'bg-red-100 text-red-800',
  ACTIVE:           'bg-green-100 text-green-800',
  TRIAL:            'bg-amber-100 text-amber-800',
  EXPIRED:          'bg-orange-100 text-orange-800',
  CANCELLED:        'bg-gray-100 text-gray-600',
  NONE:             'bg-gray-100 text-gray-400',
  UNDERPAID:        'bg-amber-100 text-amber-800',
  PENDING_APPROVAL: 'bg-yellow-100 text-yellow-800',
}

interface Props {
  status?: string | null
  className?: string
}

export function StatusBadge({ status, className }: Props) {
  const key = (status || 'NONE').toUpperCase()
  const style = STATUS_STYLES[key] || 'bg-gray-100 text-gray-500'
  const label = (status || 'NONE').replace(/_/g, ' ')
  return (
    <span className={cn('inline-block text-[0.7rem] font-semibold px-2 py-0.5 rounded-full', style, className)}>
      {label}
    </span>
  )
}
