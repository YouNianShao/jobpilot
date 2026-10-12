(() => {
  const out = [];
  document.querySelectorAll('li[role=listitem]').forEach((li) => {
    const pick = (sel) => { const n = li.querySelector(sel); return n ? (n.textContent || '').trim() : ''; };
    const box = [...li.querySelectorAll('.name-box span')].map((s) => (s.textContent || '').trim());
    const unread = li.querySelector('.unread-count') || li.querySelector('.badge-count');
    out.push({
      time: pick('.time'),
      name: box[0] || '',
      company: box[1] || '',
      hr_title: box[2] || '',
      last: pick('.last-msg-text'),
      status: pick('.message-status'),
      unread: unread ? (unread.textContent || '').trim() : ''
    });
  });
  return JSON.stringify(out);
})()