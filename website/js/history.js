'use strict';

const token = sessionStorage.getItem('access_token');
if (!token) window.location.href = '/auth';

let allTickets = [];
let persons    = [];

async function load() {
  try {
    const [ticketsRes, personsRes] = await Promise.all([
      fetch(`${API_BASE_URL}/subscriptions/history`, { headers: { Authorization: `Bearer ${token}` } }),
      fetch(`${API_BASE_URL}/persons`,               { headers: { Authorization: `Bearer ${token}` } }),
    ]);
    if (ticketsRes.status === 401) { window.location.href = '/auth'; return; }

    allTickets = await ticketsRes.json();
    persons    = await personsRes.json();

    buildFilter();
    render('all');
  } catch {
    document.getElementById('history-list').innerHTML =
      '<p class="text-center text-gray-400 py-10">Failed to load. Please refresh.</p>';
  }
}

function buildFilter() {
  const sel = document.getElementById('person-filter');
  sel.innerHTML = '<option value="all">All accounts</option>' +
    persons.map(p => `<option value="${p.person_id}">${p.display_name}</option>`).join('');
  sel.addEventListener('change', () => render(sel.value));
}

function render(filterValue) {
  const list = document.getElementById('history-list');
  const empty = document.getElementById('history-empty');

  let tickets = allTickets;
  if (filterValue !== 'all') {
    const pid = parseInt(filterValue);
    tickets = allTickets.filter(t => t.persons.some(p => p.person_id === pid));
  }

  if (!tickets.length) {
    list.innerHTML = '';
    empty.classList.remove('hidden');
    return;
  }
  empty.classList.add('hidden');

  list.innerHTML = tickets.map(t => {
    const date = t.resolved_at ? t.resolved_at.slice(0, 10) : '—';
    const persons = t.persons.map(p =>
      `<span class="inline-block bg-gray-100 text-gray-600 text-xs px-2 py-0.5 rounded-full">
        ${p.display_name} · ₹${p.amount.toLocaleString('en-IN')}
      </span>`
    ).join('');
    return `
      <div class="border border-gray-200 rounded-xl px-4 py-4">
        <div class="flex items-center justify-between mb-2">
          <div class="text-sm font-semibold text-gray-900">₹${t.total.toLocaleString('en-IN')}</div>
          <div class="text-xs text-gray-400">${date}</div>
        </div>
        <div class="flex flex-wrap gap-1">${persons}</div>
      </div>`;
  }).join('');
}

load();
