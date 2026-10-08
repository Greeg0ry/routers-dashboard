const form = document.getElementById('form');
const err = document.getElementById('err');
const submit = document.getElementById('submit');

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  err.textContent = '';
  submit.disabled = true;
  try {
    const r = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'monit' },
      body: JSON.stringify({ username: form.username.value.trim(), password: form.password.value }),
    });
    if (r.ok) {
      location.href = '/';
      return;
    }
    const body = await r.json().catch(() => ({}));
    err.textContent = body.error || 'Не удалось войти';
  } catch (_) {
    err.textContent = 'Нет связи с сервером';
  }
  submit.disabled = false;
});
