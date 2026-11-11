const history = document.querySelector('.home-chat-history');
history.scrollTop = history.scrollHeight;
const form = document.getElementById('home-chat-form');
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = document.getElementById('home-chat-input');
  const status = document.getElementById('home-chat-status');
  const button = form.querySelector('button');
  button.disabled = true;
  status.textContent = 'Thinking…';
  try {
    const response = await fetch('/home/chat', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text: input.value})});
    const result = await response.json();
    if (!response.ok) { status.textContent = result.error || 'Request failed.'; return; }
    location.reload();
  } catch (error) {
    status.textContent = 'Could not reach Unstuck. Please try again.';
  } finally { button.disabled = false; }
});
