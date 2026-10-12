(() => {
  const out = { url: location.href, title: document.title };
  out.counts = {
    listItem: document.querySelectorAll('li[role=listitem]').length,
    nameText: document.querySelectorAll('.name-text').length,
    nameBox: document.querySelectorAll('.name-box').length,
    lastMsg: document.querySelectorAll('.last-msg-text').length,
    msgStatus: document.querySelectorAll('.message-status').length,
    unread: document.querySelectorAll('.unread-count, .badge-count, [class*="unread"]').length,
    chatMessage: document.querySelectorAll('.chat-message, .message-item').length
  };
  const li = document.querySelector('li[role=listitem]');
  out.sample = li ? li.outerHTML.slice(0, 900) : null;
  out.head = (document.body.innerText || '').replace(/\s+/g,' ').slice(0, 200);
  return JSON.stringify(out, null, 1);
})()