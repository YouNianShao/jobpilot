(() => {
  const S = {
    job51:  ['.joblist-item', '.joblist-item-job', '.jname', 'a.comp .cname', '.sal', '.area'],
    liepin: ['.job-card-pc-container', 'a[data-nick="job-detail-job-info"]', 'div[data-nick^="job-detail-comp"]'],
    zhaopin:[ '.joblist-box__item', 'a.jobinfo__name', '.companyinfo__name', '.jobinfo__salary', '.jobinfo__other-info-item']
  };
  const out = { url: location.href, title: document.title, counts: {}, samples: {} };
  for (const k of Object.keys(S)) {
    out.counts[k] = {};
    for (const s of S[k]) out.counts[k][s] = document.querySelectorAll(s).length;
  }
  const first = (s) => { const el = document.querySelector(s); return el ? el.outerHTML.slice(0, 900) : null; };
  out.samples.job51 = first('.joblist-item');
  out.samples.liepin = first('.job-card-pc-container');
  out.samples.zhaopin = first('.joblist-box__item');
  out.loginWall = !!document.querySelector('form[action*=login], .login-dialog, [class*=login-dialog], .qrcode-login, [class*=qrcode]');
  out.bodyHead = (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 220);
  return JSON.stringify(out, null, 1);
})()