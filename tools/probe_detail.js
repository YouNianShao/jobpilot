(() => {
  const codes = (el) => el ? Array.from(el.textContent || '').map(c => c.codePointAt(0).toString(16)) : null;
  const s = document.querySelector('.info-primary .salary') || document.querySelector('.salary');
  const out = {
    url: location.href,
    title: document.title,
    counts: {
      jobSecText: document.querySelectorAll('.job-sec-text').length,
      nameH1: document.querySelectorAll('.info-primary .name h1, .name h1').length,
      salary: document.querySelectorAll('.info-primary .salary, .salary').length,
      tagSpan: document.querySelectorAll('.info-primary .tag-list span').length,
      companyInfoA: document.querySelectorAll('.sider-company .company-info a').length,
      bossInfoAttr: document.querySelectorAll('.boss-info-attr').length,
      jobBossInfo: document.querySelectorAll('.job-boss-info').length,
      startChat: document.querySelectorAll('.btn-startchat, .op-btn-chat, [ka*="chat"], [ka*="go_chat"]').length,
      jdText: document.querySelectorAll('.job-sec-text').length
    },
    salaryText: s ? s.textContent : null,
    salaryCodes: codes(s),
    salaryHTML: s ? s.outerHTML.slice(0, 300) : null,
    salaryFont: s ? getComputedStyle(s).fontFamily : null,
    pseudoBefore: s ? getComputedStyle(s, '::before').content : null,
    jdHead: (document.querySelector('.job-sec-text')?.innerText || '').replace(/\s+/g,' ').slice(0, 240),
    hrName: (document.querySelector('.boss-info-attr .name, .job-boss-info .name')?.textContent || '').trim(),
    company: (document.querySelector('.sider-company .company-info a')?.textContent || '').trim()
  };
  return JSON.stringify(out, null, 1);
})()