document.querySelectorAll('form[data-pending]').forEach(form => {
  form.addEventListener('submit', () => {
    const button = form.querySelector('button');
    button.disabled = true;
    button.textContent = form.dataset.pending;
    form.setAttribute('aria-busy', 'true');
  });
});
window.addEventListener('pageshow', event => { if (event.persisted) window.location.reload(); });
