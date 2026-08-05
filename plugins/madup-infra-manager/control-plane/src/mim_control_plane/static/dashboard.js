async function refreshReadonlyPanels() {
  await Promise.all([
    fetch('/v1/workloads', { credentials: 'same-origin' }),
    fetch('/v1/usage', { credentials: 'same-origin' }),
  ]);
}

window.addEventListener('load', () => {
  void refreshReadonlyPanels();
});
