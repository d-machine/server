import { NavLink, useNavigate } from 'react-router-dom'
import { clearAdminCreds } from '@/lib/auth'
import { cn } from '@/lib/utils'

interface Props { username: string }

const links = [
  { to: '/',              label: 'Dashboard',    end: true },
  { to: '/tickets',       label: 'Past Tickets'           },
  { to: '/users',         label: 'Users'                  },
  { to: '/persons',       label: 'Persons'                },
]

export function Navbar({ username }: Props) {
  const navigate = useNavigate()

  function logout() {
    clearAdminCreds()
    navigate('/login')
  }

  return (
    <nav className="bg-slate-800 sticky top-0 z-40">
      <div className="max-w-[1200px] mx-auto px-5 h-14 flex items-center gap-6">
        <span className="font-bold text-white text-[1.05rem] tracking-tight shrink-0">
          ArthaDesk Admin
        </span>
        <div className="flex items-center gap-1 flex-1">
          {links.map(l => (
            <NavLink
              key={l.to}
              to={l.to}
              end={l.end}
              className={({ isActive }) =>
                cn('text-sm px-3 py-1.5 rounded-md transition-colors whitespace-nowrap',
                  isActive
                    ? 'bg-white/10 text-white font-semibold'
                    : 'text-slate-300 hover:text-white hover:bg-white/10')
              }
            >
              {l.label}
            </NavLink>
          ))}
        </div>
        <div className="flex items-center gap-3 shrink-0">
          <span className="text-sm text-slate-400 hidden sm:inline">{username}</span>
          <button
            onClick={logout}
            className="text-sm text-slate-300 hover:text-white border border-slate-600 hover:border-slate-400 px-3 py-1.5 rounded-md transition-colors"
          >
            Logout
          </button>
        </div>
      </div>
    </nav>
  )
}
