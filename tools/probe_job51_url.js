(() => {
  const out = { url: location.href, title: document.title };
  out.counts = {
    jobSec: document.querySelectorAll('.job-sec-text, .job-detail, .tCompany_main, .bmsg').length,
    jobName: document.querySelectorAll('h1, .cn h1, .tHeader h1').length
  };
  out.head = (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 200);
  return JSON.stringify(out, null, 1);
})()