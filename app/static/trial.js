const material = document.querySelector('.materials');
if (material) {
  const sample = material.querySelector('#trial-sample');
  const draft = material.querySelector('#trial-input');
  sample.addEventListener('change', () => {
    if (sample.value === 'custom') {
      draft.open = true;
      draft.querySelector('input').focus();
    }
  });
  material.addEventListener('submit', () => {
    material.querySelector('#pending').textContent = '称量计算中…';
  });
}

document.querySelectorAll('form[data-api="json"]').forEach((form) => {
  const note = document.querySelector('[data-api-note]');
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const payload = {};
    new FormData(form).forEach((value, key) => {
      if (value !== '') payload[key] = key === 'quantity_g' || key === 'version' ? Number(value) : value;
    });
    if (!('version' in payload) && form.action.endsWith('/invoices')) delete payload.version;
    try {
      const reply = await fetch(form.action, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const body = await reply.json();
      if (!reply.ok) throw new Error(body.code || `HTTP ${reply.status}`);
      if (note) note.textContent = `已入账（${body.created ? '新事件' : '幂等复用'}），刷新页面查看关账视图。`;
      setTimeout(() => window.location.reload(), 700);
    } catch (error) {
      if (note) note.textContent = `提交失败：${error.message}`;
    }
  });
});
