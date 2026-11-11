const history = document.querySelector('.home-chat-history');
const toBottom = () => { history.scrollTop = history.scrollHeight; };
toBottom(); window.addEventListener('load', toBottom);
const form = document.getElementById('home-chat-form');
const input = document.getElementById('home-chat-input');
const status = document.getElementById('home-chat-status');

function bubble(cls, label, text) {
  const li = document.createElement('li');
  li.className = 'msg ' + cls;
  if (cls.includes('bot')) {
    const av = document.createElement('span'); av.className = 'avatar'; av.setAttribute('aria-hidden', 'true'); av.textContent = 'U';
    li.append(av);
  }
  const wrap = document.createElement('div');
  const who = document.createElement('span'); who.className = 'who-label'; who.textContent = label;
  const b = document.createElement('div'); b.className = 'bubble';
  if (text === null) { b.innerHTML = '<span class="dots" aria-label="Thinking"><span></span><span></span><span></span></span>'; }
  else { b.textContent = text; }
  wrap.append(who, b);
  if (cls.includes('me')) { li.append(who, b); } else { li.append(wrap); }
  history.querySelector('.hc-empty')?.remove();
  history.append(li);
  history.scrollTop = history.scrollHeight;
  return li;
}

document.querySelectorAll('.hc-suggest .chip').forEach((chip) => chip.addEventListener('click', () => {
  input.value = chip.dataset.q;
  form.requestSubmit();
}));

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  const controls = form.querySelectorAll('button, input');
  document.querySelectorAll('.hc-suggest .chip').forEach((c) => { c.disabled = true; });
  controls.forEach((c) => { c.disabled = true; });
  status.textContent = '';
  window.homeChatBusy = true;
  bubble('me', 'You', text);
  const pending = bubble('bot pending', 'Unstuck', null);
  try {
    const response = await fetch('/home/chat', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text})});
    const result = await response.json();
    if (!response.ok) { pending.remove(); status.textContent = result.error || 'Request failed.'; return; }
    location.reload();
  } catch (error) {
    pending.remove();
    status.textContent = 'Could not reach Unstuck. Please try again.';
  } finally {
    controls.forEach((c) => { c.disabled = false; });
    document.querySelectorAll('.hc-suggest .chip').forEach((c) => { c.disabled = false; });
    window.homeChatBusy = false;
  }
});
