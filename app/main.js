const { invoke } = window.__TAURI__.core;

// Tab switching
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    const target = tab.dataset.tab;

    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));

    tab.classList.add('active');
    document.getElementById(`screen-${target}`).classList.add('active');
  });
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
