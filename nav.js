// nav.js — shared Update All logic, loaded by every page

function startUpdateAll() {
  const btn = document.getElementById('btn-update-all');
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  btn.textContent = 'Starting…';
  fetch('/refresh-all', { method: 'POST' })
    .then(r => r.json())
    .then(d => { if (d.error) resetUpdateBtn(); else pollUpdateAll(); })
    .catch(resetUpdateBtn);
}

function pollUpdateAll() {
  fetch('/refresh-all-status')
    .then(r => r.json())
    .then(d => {
      const btn = document.getElementById('btn-update-all');
      if (!btn) return;
      if (d.running) {
        btn.textContent = `Updating… ${d.done}/${d.total}`;
        setTimeout(pollUpdateAll, 2000);
      } else {
        btn.textContent = d.total > 0 ? `Done (${d.total})` : 'Update All';
        btn.disabled = false;
        if (d.total > 0) setTimeout(() => { if (btn) btn.textContent = 'Update All'; }, 3000);
      }
    })
    .catch(resetUpdateBtn);
}

function resetUpdateBtn() {
  const btn = document.getElementById('btn-update-all');
  if (btn) { btn.textContent = 'Update All'; btn.disabled = false; }
}

// On page load, reconnect if an update is already running
document.addEventListener('DOMContentLoaded', () => {
  fetch('/refresh-all-status')
    .then(r => r.json())
    .then(d => {
      if (!d.running) return;
      const btn = document.getElementById('btn-update-all');
      if (btn) { btn.disabled = true; btn.textContent = `Updating… ${d.done}/${d.total}`; }
      pollUpdateAll();
    })
    .catch(() => {});
});
