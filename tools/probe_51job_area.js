(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const out = { url: location.href, title: document.title };
  for (let i = 0; i < 12; i++) {
    if (document.querySelectorAll('.joblist-item').length) break;
    if (/没有找到|无结果|暂无/.test((document.body.innerText || '').slice(0, 400))) break;
    await sleep(700);
  }
  const items = [...document.querySelectorAll('.joblist-item')].slice(0, 4).map((item) => {
    const node = item.querySelector('.joblist-item-job') || item;
    let data = {};
    try { data = JSON.parse(node.getAttribute('sensorsdata') || '{}'); } catch (e) {}
    const name = item.querySelector('.jname');
    const comp = item.querySelector('a.comp .cname') || item.querySelector('a.comp');
    return {
      title: (name ? name.innerText : '').trim().slice(0, 30),
      company: (comp ? comp.innerText : '').trim().slice(0, 20),
      area: data.jobArea || '',
      salary: data.jobSalary || '',
    };
  });
  out.count = document.querySelectorAll('.joblist-item').length;
  out.items = items;
  out.bodyHead = (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 160);
  out.empty = /没有找到|无结果|暂无相关/.test((document.body.innerText || '').slice(0, 600));
  return JSON.stringify(out, null, 1);
})()