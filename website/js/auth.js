/* jshint esversion: 11 */
'use strict';

function showLogin()    {
  document.getElementById('view-login').classList.remove('hidden');
  document.getElementById('view-register').classList.add('hidden');
  document.getElementById('view-forgot').classList.add('hidden');
}
function showRegister() {
  document.getElementById('view-register').classList.remove('hidden');
  document.getElementById('view-login').classList.add('hidden');
  document.getElementById('view-forgot').classList.add('hidden');
}
function showForgot()   {
  document.getElementById('view-forgot').classList.remove('hidden');
  document.getElementById('view-login').classList.add('hidden');
  document.getElementById('view-register').classList.add('hidden');
}

function showError(id, msg)    { const el = document.getElementById(id); el.textContent = msg; el.classList.remove('hidden'); }
function hideError(id)         { document.getElementById(id).classList.add('hidden'); }
function showSuccess(id, msg)  { const el = document.getElementById(id); el.textContent = msg; el.classList.remove('hidden'); }

// Expose for onclick attributes
window.showLogin    = showLogin;
window.showRegister = showRegister;
window.showForgot   = showForgot;

// Route based on hash
if (location.hash === '#register') showRegister();
else if (location.hash === '#forgot') showForgot();

// Login
document.getElementById('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  hideError('login-error');
  const btn = e.target.querySelector('button[type=submit]');
  btn.disabled = true; btn.textContent = 'Logging in…';
  try {
    const res = await fetch(`${API_BASE_URL}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        email:    document.getElementById('login-email').value,
        password: document.getElementById('login-password').value,
      }),
    });
    if (!res.ok) { const d = await res.json(); throw new Error(d.detail || 'Login failed'); }
    const data = await res.json();
    sessionStorage.setItem('access_token',  data.access_token);
    sessionStorage.setItem('refresh_token', data.refresh_token);
    sessionStorage.setItem('email',         document.getElementById('login-email').value);
    window.location.href = '/account';
  } catch (err) {
    showError('login-error', err.message);
    btn.disabled = false; btn.textContent = 'Login';
  }
});

// Forgot password
document.getElementById('forgot-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  hideError('forgot-error');
  const btn = e.target.querySelector('button[type=submit]');
  btn.disabled = true; btn.textContent = 'Sending…';
  try {
    await fetch(`${API_BASE_URL}/auth/forgot-password`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: document.getElementById('forgot-email').value }),
    });
    showSuccess('forgot-success', 'If this email is registered, a reset link has been sent. Check your inbox.');
  } catch {
    showError('forgot-error', 'Something went wrong. Please try again.');
  } finally {
    btn.disabled = false; btn.textContent = 'Send reset link';
  }
});

// Register
document.getElementById('register-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  hideError('register-error');
  const pw = document.getElementById('reg-password').value;
  if (pw !== document.getElementById('reg-confirm').value) {
    showError('register-error', 'Passwords do not match');
    return;
  }
  const btn = e.target.querySelector('button[type=submit]');
  btn.disabled = true; btn.textContent = 'Creating account…';
  try {
    const res = await fetch(`${API_BASE_URL}/auth/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name:     document.getElementById('reg-name').value,
        email:    document.getElementById('reg-email').value,
        password: pw,
      }),
    });
    if (!res.ok) { const d = await res.json(); throw new Error(d.detail || 'Registration failed'); }
    // Auto-login after register
    const login = await fetch(`${API_BASE_URL}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: document.getElementById('reg-email').value, password: pw }),
    });
    const data = await login.json();
    sessionStorage.setItem('access_token',  data.access_token);
    sessionStorage.setItem('refresh_token', data.refresh_token);
    sessionStorage.setItem('email',         document.getElementById('reg-email').value);
    window.location.href = '/account';
  } catch (err) {
    showError('register-error', err.message);
    btn.disabled = false; btn.textContent = 'Create Account';
  }
});
