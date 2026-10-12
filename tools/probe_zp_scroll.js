(async () => {
  const out = { url: location.href };
  const count = () => document.querySelectorAll('.job-card').length;
  const titles = () => Array.from(document.querySelectorAll('.job-card [aria-label]')).map((el) => el.getAttribute('aria-label'));
  out.before = count();
  out.sampleBefore = titles().slice(0, 3);
  for (let i = 0; i < 5; i++) {
    window.scrollTo(0, document.body.scrollHeight);
    await new Promise((resolve) => setTimeout(resolve, 2500));
    out['after' + (i + 1)] = count();
  }
  out.sampleAfter = titles().slice(-3);
  out.unique = new Set(titles()).size;
  return JSON.stringify(out, null, 1);
})()