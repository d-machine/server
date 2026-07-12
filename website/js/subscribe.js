'use strict';

const token = sessionStorage.getItem('access_token');
if (!token) window.location.href = '/auth';

let selectedFile = null;
let persons = [];

// File upload
const dropZone  = document.getElementById('drop-zone');
const fileInput = document.getElementById('screenshot-file');
dropZone.addEventListener('click', () => fileInput.click());
dropZone.addEventListener('dragover',  e => { e.preventDefault(); dropZone.classList.add('border-blue-400'); });
dropZone.addEventListener('dragleave', ()  => dropZone.classList.remove('border-blue-400'));
dropZone.addEventListener('drop', e => { e.preventDefault(); handleFile(e.dataTransfer.files[0]); });
fileInput.addEventListener('change', () => handleFile(fileInput.files[0]));

function handleFile(file) {
  if (!file) return;
  selectedFile = file;
  document.getElementById('file-label').innerHTML =
    `<div class="text-green-600 font-medium">✓ ${file.name}</div>` +
    `<div class="text-xs text-gray-400 mt-1">${(file.size / 1024).toFixed(0)} KB</div>`;
  loadPersons();
}

async function loadPersons() {
  try {
    const res = await fetch(`${API_BASE_URL}/persons`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (res.status === 401) { window.location.href = '/auth'; return; }
    persons = await res.json();
    renderPersons();
    document.getElementById('persons-section').classList.remove('hidden');
  } catch {
    showError('Could not load portfolio accounts. Please try again.');
  }
}

function renderPersons() {
  const grid  = document.getElementById('persons-grid');
  const noMsg = document.getElementById('no-persons-msg');
  if (!persons.length) { noMsg.classList.remove('hidden'); return; }

  grid.innerHTML = persons.map((p, i) => {
    const statusBadge = p.subscription_status === 'ACTIVE'
      ? '<span class="text-xs bg-green-100 text-green-700 px-2 py-0.5 rounded-full">Active</span>'
      : p.subscription_status === 'PENDING_APPROVAL'
        ? '<span class="text-xs bg-yellow-100 text-yellow-700 px-2 py-0.5 rounded-full">Pending</span>'
        : '<span class="text-xs bg-gray-100 text-gray-500 px-2 py-0.5 rounded-full">No subscription</span>';
    return `
      <div class="flex items-center gap-4 border border-gray-200 rounded-xl px-4 py-3">
        <input type="checkbox" id="person-${i}" data-person-id="${p.person_id}"
          class="person-check w-4 h-4 text-blue-600 rounded cursor-pointer"
          onchange="onPersonToggle(this, ${i})">
        <label for="person-${i}" class="flex-1 cursor-pointer">
          <div class="font-medium text-gray-900">${p.display_name}</div>
          <div class="flex items-center gap-2 mt-0.5">${statusBadge}</div>
        </label>
        <div class="flex items-center gap-1">
          <span class="text-gray-400 text-sm">₹</span>
          <input type="number" id="amount-${i}" value="1000" min="1"
            class="person-amount w-24 border border-gray-300 rounded-lg px-2 py-1.5 text-sm text-right disabled:bg-gray-50 disabled:text-gray-400"
            disabled
            oninput="updateTotal()">
        </div>
      </div>`;
  }).join('');

  updateTotal();
}

function onPersonToggle(cb, i) {
  document.getElementById(`amount-${i}`).disabled = !cb.checked;
  updateTotal();
  updateSubmitBtn();
}
window.onPersonToggle = onPersonToggle;

function updateTotal() {
  let total = 0;
  document.querySelectorAll('.person-check:checked').forEach(cb => {
    const i = cb.id.split('-')[1];
    total += parseInt(document.getElementById(`amount-${i}`).value) || 0;
  });
  document.getElementById('total-amount').textContent = '₹' + total.toLocaleString('en-IN');
}
window.updateTotal = updateTotal;

function updateSubmitBtn() {
  const hasFile    = !!selectedFile;
  const hasPersons = document.querySelectorAll('.person-check:checked').length > 0;
  document.getElementById('submit-btn').disabled = !(hasFile && hasPersons);
}

document.getElementById('submit-btn').addEventListener('click', async () => {
  hideMessages();
  const btn = document.getElementById('submit-btn');
  btn.disabled = true; btn.textContent = 'Uploading…';

  const personsPayload = [];
  document.querySelectorAll('.person-check:checked').forEach(cb => {
    const i = cb.id.split('-')[1];
    personsPayload.push({
      person_id: parseInt(cb.dataset.personId),
      amount:    parseInt(document.getElementById(`amount-${i}`).value) || 1000,
    });
  });

  const form = new FormData();
  form.append('persons',    JSON.stringify(personsPayload));
  form.append('screenshot', selectedFile);

  try {
    const res = await fetch(`${API_BASE_URL}/tickets/submit`, {
      method:  'POST',
      headers: { Authorization: `Bearer ${token}` },
      body:    form,
    });
    if (!res.ok) { const d = await res.json(); throw new Error(d.detail || 'Submission failed'); }
    showSuccess('✅ Screenshot submitted! Verification usually takes a few hours. You\'ll receive an email once your subscription is activated.');
    btn.textContent = 'Submitted';
  } catch (err) {
    showError(err.message);
    btn.disabled = false; btn.textContent = 'Submit for Verification';
    updateSubmitBtn();
  }
});

function showError(msg)   { const el = document.getElementById('error-banner');   el.textContent = msg; el.classList.remove('hidden'); }
function showSuccess(msg) { const el = document.getElementById('success-banner'); el.textContent = msg; el.classList.remove('hidden'); }
function hideMessages()   {
  document.getElementById('error-banner').classList.add('hidden');
  document.getElementById('success-banner').classList.add('hidden');
}
