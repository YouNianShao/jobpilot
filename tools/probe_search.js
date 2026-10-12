(() => {
  const out = { url: location.href, title: document.title };
  out.counts = {
    jobCardWrap: document.querySelectorAll('.job-card-wrap').length,
    jobCardBox: document.querySelectorAll('.job-card-box').length,
    jobName: document.querySelectorAll('.job-name').length,
    jobSalary: document.querySelectorAll('.job-salary').length,
    tagListLi: document.querySelectorAll('.tag-list li').length,
    recJobList: document.querySelectorAll('.rec-job-list').length,
    bossName: document.querySelectorAll('.boss-name').length,
    companyLocation: document.querySelectorAll('.company-location').length,
    anyLoginForm: document.querySelectorAll('form[action*=login], .login-form, [class*=login-dialog]').length
  };
  const w = document.querySelector('.job-card-wrap');
  out.sampleCard = w ? w.outerHTML.slice(0, 1500) : null;
  out.bodyHead = (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 300);
  return JSON.stringify(out, null, 1);
})()