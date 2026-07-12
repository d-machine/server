'use strict';

const token = sessionStorage.getItem('access_token');
if (!token) window.location.href = '/auth';

let panSalt = '';

function authHeader() {
  return { Authorization: `Bearer ${token}` };
}

async function load() {
  try {
    const [meRes, personsRes, subRes] = await Promise.all([
      fetch(`${API_BASE_URL}/auth/me`,               { headers: authHeader() }),
      fetch(`${API_BASE_URL}/persons`,               { headers: authHeader() }),
      fetch(`${API_BASE_URL}/subscriptions/status`,  { headers: authHeader() }),
    ]);
    if (meRes.status === 401 || personsRes.status === 401) { window.location.href = '/auth'; return; }

    const me      = await meRes.json();
    const persons = await personsRes.json();
    const sub     = await subRes.json();

    panSalt = me.pan_salt || '';
    document.getElementById('acc-email').textContent = me.email;
    document.getElementById('acc-name').textContent  = me.name;

    renderPersons(persons, sub);
  } catch {
    document.getElementById('persons-loading').textContent = 'Failed to load. Please refresh.';
  }
}

// Build a map of person_id → subscription detail from the status response
function buildSubMap(sub) {
  const map = {};
  if (sub.persons) {
    for (const p of sub.persons) {
      map[p.person_id] = p;
    }
  }
  return map;
}

function statusLine(personSub) {
  if (!personSub) {
    return { html: '<span class="text-xs text-gray-400">No subscription</span>', highlight: '' };
  }

  const status     = (personSub.status || 'NONE').toUpperCase();
  const expiresAt  = personSub.expires_at;
  const paidPrice  = personSub.paid_price;
  const reqPrice   = personSub.required_price;
  const date       = expiresAt ? expiresAt.slice(0, 10) : null;

  const paidFY   = personSub.paid_this_fy;
  const paidText = (paidFY != null && paidFY > 0)
    ? ` · ₹${paidFY.toLocaleString('en-IN')} paid this year`
    : '';

  if (status === 'ACTIVE') {
    return {
      html: `<span class="text-xs text-green-600">Active until ${date ?? '—'}${paidText}</span>`,
      highlight: '',
    };
  }

  if (status === 'TRIAL') {
    return {
      html: `<span class="text-xs text-amber-600 font-medium">Pay by ${date ?? '—'}${paidText}</span>`,
      highlight: 'border-amber-200 bg-amber-50',
    };
  }

  if (status === 'PENDING_APPROVAL') {
    return {
      html: `<span class="text-xs text-yellow-600">Awaiting verification</span>`,
      highlight: '',
    };
  }

  if (status === 'EXPIRED') {
    const daysOverdue = date
      ? Math.floor((Date.now() - new Date(date).getTime()) / 86400000)
      : null;
    const overdueText = daysOverdue != null
      ? `Overdue by ${daysOverdue} day${daysOverdue !== 1 ? 's' : ''}`
      : 'Overdue';
    return {
      html: `<span class="text-xs text-red-600 font-medium">${overdueText} · was due ${date ?? '—'}</span>`,
      highlight: 'border-red-200 bg-red-50',
    };
  }

  if (status === 'UNDERPAID') {
    const shortfall = (reqPrice != null && paidPrice != null) ? reqPrice - paidPrice : null;
    const shortText = shortfall != null ? ` · ₹${shortfall.toLocaleString('en-IN')} remaining` : '';
    return {
      html: `<span class="text-xs text-amber-700 font-medium">Underpaid${shortText}</span>`,
      highlight: 'border-amber-200 bg-amber-50',
    };
  }

  if (status === 'CANCELLED') {
    return {
      html: `<span class="text-xs text-gray-400">Cancelled</span>`,
      highlight: '',
    };
  }

  return { html: '<span class="text-xs text-gray-400">No subscription</span>', highlight: '' };
}

function renderPersons(persons, sub) {
  document.getElementById('persons-loading').classList.add('hidden');
  const list = document.getElementById('persons-list');

  if (!persons.length) {
    document.getElementById('persons-empty').classList.remove('hidden');
    return;
  }

  const subMap = buildSubMap(sub);

  const HIGHLIGHT_STYLES = {
    'border-amber-200 bg-amber-50': 'background:#fffbeb;border:1px solid #fde68a;border-radius:12px;margin:4px 0;padding:12px;',
    'border-red-200 bg-red-50':     'background:#fef2f2;border:1px solid #fecaca;border-radius:12px;margin:4px 0;padding:12px;',
  };

  list.innerHTML = persons.map((p, idx) => {
    const isLast = idx === persons.length - 1;
    const personSub = subMap[p.person_id] || null;
    const { html: statusHtml, highlight } = statusLine(personSub);

    const rowStyle = highlight
      ? HIGHLIGHT_STYLES[highlight] || 'padding:12px;'
      : `padding:14px 0;${!isLast ? 'border-bottom:1px solid #d1d5db;' : ''}`;

    return `
      <div style="display:flex;align-items:center;justify-content:space-between;${rowStyle}">
        <div>
          <div style="font-weight:500;color:#111827;">${p.display_name}</div>
          <div style="margin-top:2px;">${statusHtml}</div>
        </div>
      </div>`;
  }).join('');
}

function toggleAddForm() {
  const form = document.getElementById('add-person-form');
  form.classList.toggle('hidden');
  document.getElementById('add-person-error').classList.add('hidden');
  document.getElementById('add-person-success').classList.add('hidden');
}
window.toggleAddForm = toggleAddForm;

async function hashPAN(pan) {
  const combined = pan.toUpperCase().trim() + panSalt;
  const buf = await crypto.subtle.digest('SHA-512', new TextEncoder().encode(combined));
  return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
}

function maskedPAN(pan) {
  pan = pan.toUpperCase().trim();
  if (pan.length !== 10) return null;
  return pan.slice(0, 5) + '****' + pan.slice(9);
}

async function submitAddPerson() {
  document.getElementById('add-person-error').classList.add('hidden');
  document.getElementById('add-person-success').classList.add('hidden');

  const displayName = document.getElementById('new-display-name').value.trim();
  const pan         = document.getElementById('new-pan').value.trim().toUpperCase();

  if (!displayName) { showAddError('Please enter a display name.'); return; }
  if (!/^[A-Z]{5}[0-9]{4}[A-Z]$/.test(pan)) { showAddError('Please enter a valid PAN (e.g. ABCDE1234F).'); return; }

  const masked  = maskedPAN(pan);
  const panHash = await hashPAN(pan);

  try {
    const res = await fetch(`${API_BASE_URL}/persons`, {
      method: 'POST',
      headers: { ...authHeader(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ pan_hash: panHash, masked_pan: masked, display_name: displayName }),
    });
    if (res.status === 409) { showAddError('This PAN is already registered under your account.'); return; }
    if (!res.ok) { const d = await res.json(); throw new Error(d.detail || 'Registration failed'); }
    document.getElementById('add-person-success').textContent = `${displayName} registered successfully.`;
    document.getElementById('add-person-success').classList.remove('hidden');
    document.getElementById('new-display-name').value = '';
    document.getElementById('new-pan').value = '';
    load();
  } catch (err) {
    showAddError(err.message);
  }
}
window.submitAddPerson = submitAddPerson;

function showAddError(msg) {
  const el = document.getElementById('add-person-error');
  el.textContent = msg;
  el.classList.remove('hidden');
}

load();
