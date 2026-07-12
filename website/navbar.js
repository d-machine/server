(function () {
  const token = sessionStorage.getItem('access_token');
  const email = sessionStorage.getItem('email') || '';
  const path  = location.pathname.replace(/\/$/, '') || '/';

  function isActive(href) {
    return path === href
      ? 'text-white bg-white/10 font-medium'
      : 'text-slate-300 hover:text-white hover:bg-white/10';
  }

  const publicLinks = `
    <a href="/pricing" class="text-sm px-3 py-1.5 rounded-md transition-colors ${isActive('/pricing')}">Pricing</a>`;

  const authLinks = token
    ? `<a href="/account" class="text-sm px-3 py-1.5 rounded-md transition-colors ${isActive('/account')}">My Account</a>
       <a href="/subscribe" class="text-sm px-3 py-1.5 rounded-md transition-colors ${isActive('/subscribe')}">Subscribe</a>`
    : '';

  const rightLinks = token
    ? `<span class="text-sm text-slate-400 hidden sm:inline">${email}</span>
       <button onclick="__navLogout()" class="text-sm text-slate-300 hover:text-white border border-slate-600 hover:border-slate-400 px-3 py-1.5 rounded-md transition-colors">Logout</button>`
    : `<a href="/auth" class="text-sm text-slate-300 hover:text-white px-3 py-1.5 rounded-md hover:bg-white/10 transition-colors">Login</a>
       <a href="/auth#register" class="text-sm bg-blue-500 text-white rounded-lg px-4 py-1.5 hover:bg-blue-400 transition-colors">Register</a>`;

  const el = document.getElementById('navbar');
  if (!el) return;

  el.innerHTML = `
    <div class="w-full px-6 h-14 flex items-center gap-6">
      <a href="/" class="font-bold text-lg text-white tracking-tight shrink-0">ArthaDesk</a>
      <div class="flex gap-1 items-center flex-1">
        ${publicLinks}
        ${authLinks}
      </div>
      <div class="flex gap-2 items-center shrink-0">
        ${rightLinks}
      </div>
    </div>`;

  el.style.cssText = 'background:#1e293b; position:sticky; top:0; z-index:40;';

  window.__navLogout = function () {
    const rt = sessionStorage.getItem('refresh_token');
    if (rt) {
      fetch(`${API_BASE_URL}/auth/logout`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: rt }),
      }).catch(() => {});
    }
    sessionStorage.clear();
    window.location.href = '/auth';
  };

  window.Nav = { logout: window.__navLogout };
})();
