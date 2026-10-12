(() => {
  const out = { url: location.href, title: document.title };
  out.jobTitle = (document.querySelector('h1, .cn h1, .tHeader h1, .job-name') || {}).textContent
    ? (document.querySelector('h1, .cn h1, .tHeader h1, .job-name').textContent || '').trim().slice(0, 60) : '';
  out.hasDetail = {
    jobSec: document.querySelectorAll('.job-sec-text, .tCompany_main, .job-detail, .bmsg').length,
    salary: document.querySelectorAll('.cn strong, .tHeader strong, .salary').length
  };
  out.head = (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 160);
  return JSON.stringify(out, null, 1);
})()