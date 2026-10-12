(() => {
  const vis = (n) => { if(!n) return false; const r = n.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const out = { url: location.href, title: document.title };
  const items = [...document.querySelectorAll("li[role=listitem]")];
  out.itemCount = items.length;
  if (items.length === 0) {
    out.fallbackCounts = {};
    for (const sel of ["[role=listitem]", "[class*=user-list] li", "[class*=friend] li", "[class*=chat] li", "ul li"]) {
      out.fallbackCounts[sel] = document.querySelectorAll(sel).length;
    }
    out.bodyText = (document.body.innerText || "").replace(/\s+/g, " ").slice(0, 400);
    return JSON.stringify(out, null, 1);
  }
  const cls = new Set();
  items.slice(0, 3).forEach((li) => {
    [...li.querySelectorAll("*")].forEach((n) => {
      const c = (n.className || "").toString().trim();
      if (c) cls.add(c.split(/\s+/).slice(0, 2).join("."));
    });
  });
  out.classesInRow = [...cls].slice(0, 40);
  out.rows = items.slice(0, 8).map((li) => {
    const g = (sel) => { const n = li.querySelector(sel); return n ? (n.innerText || "").trim() : null; };
    return {
      text: (li.innerText || "").replace(/\s+/g, " ").slice(0, 120),
      name: g(".name-text"),
      box: [...li.querySelectorAll(".name-box span")].map((s) => (s.innerText || "").trim()),
      last: g(".last-msg-text"),
      status: g(".message-status"),
      unread: g(".unread-count, .badge-count, [class*=unread]"),
    };
  });
  return JSON.stringify(out, null, 1);
})()