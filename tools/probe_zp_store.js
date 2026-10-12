(async () => {
  const out = { url: location.href };
  for (let i = 0; i < 3; i++) {
    window.scrollTo(0, document.body.scrollHeight);
    await new Promise((r) => setTimeout(r, 2500));
  }
  out.cards = document.querySelectorAll('.job-card').length;

  const app = document.querySelector('#app');
  const vueApp = app && app.__vue_app__;
  out.hasVueApp = !!vueApp;
  const gp = vueApp && vueApp.config && vueApp.config.globalProperties;
  out.gpKeys = gp ? Object.keys(gp).slice(0, 25) : null;
  const pinia = gp && gp.$pinia;
  out.hasPinia = !!pinia;
  if (pinia && pinia.state && pinia.state.value) {
    out.storeKeys = Object.keys(pinia.state.value);
    let blob = '';
    try { blob = JSON.stringify(pinia.state.value); } catch (e) { blob = ''; }
    out.blobLen = blob.length;
    out.numberCount = (blob.match(/"positionNumber":"[A-Za-z0-9]{6,}"/g) || []).length;
    out.nameCount = (blob.match(/"positionName":"/g) || []).length;
    const m = blob.match(/"positionName":"[^"]{1,80}"[^}]{0,300}?"positionNumber":"[A-Za-z0-9]{6,}"/);
    out.sample = m ? m[0].slice(0, 220) : null;
  }
  return JSON.stringify(out, null, 1);
})()