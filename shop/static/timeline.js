(() => {
  const list = document.querySelector('#timeline');
  const controls = document.querySelector('#replay-controls');
  if (!list || !controls) return;
  const events = [...list.children];
  let timer;
  controls.hidden = false;
  const status = document.querySelector('#replay-status');
  const showAll = () => {
    clearInterval(timer);
    events.forEach(event => { event.hidden = false; });
    status.textContent = 'All recorded events are visible.';
  };
  document.querySelector('#show-all').addEventListener('click', showAll);
  document.querySelector('#replay').addEventListener('click', () => {
    clearInterval(timer);
    events.forEach(event => { event.hidden = true; });
    let next = 0;
    const reveal = () => {
      if (next >= events.length) { clearInterval(timer); return; }
      events[next++].hidden = false;
      status.textContent = `${next} of ${events.length} recorded events shown.`;
    };
    reveal();
    timer = setInterval(reveal, 900);
  });
})();
