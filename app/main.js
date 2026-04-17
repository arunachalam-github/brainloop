const { invoke } = window.__TAURI__.core;

const TABS = ['today', 'week', 'ask', 'settings'];

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  document.querySelector(`.tab[data-tab="${name}"]`).classList.add('active');
  document.getElementById(`screen-${name}`).classList.add('active');
}

// Click
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => switchTab(tab.dataset.tab));
});

// Arrow key navigation
document.addEventListener('keydown', (e) => {
  if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown' &&
      e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
  e.preventDefault();
  const active = document.querySelector('.tab.active');
  const current = TABS.indexOf(active?.dataset.tab);
  if (current === -1) return;
  const next = (e.key === 'ArrowDown' || e.key === 'ArrowRight')
    ? Math.min(current + 1, TABS.length - 1)
    : Math.max(current - 1, 0);
  switchTab(TABS[next]);
});

// Show row count in Settings
async function loadRowCount() {
  try {
    const count = await invoke('row_count');
    const el = document.getElementById('row-count-display');
    if (el) {
      el.textContent = `${count.toLocaleString()} activity records captured`;
    }
  } catch (e) {
    console.error('row_count failed:', e);
  }
}

loadRowCount();
