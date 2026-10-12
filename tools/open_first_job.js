(function () {
  const row = document.querySelector('#jobs tr');
  if (!row) return 'no row';
  row.click();
  return 'clicked';
})()