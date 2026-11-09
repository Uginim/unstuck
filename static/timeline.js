// Local display only: recorded events remain visible without JavaScript.
const list = document.querySelector('#timeline-list');
const items = list ? Array.from(list.querySelectorAll('li:has(time)')) : [];
if (items.length) {
  const controls = document.querySelector('#timeline-controls');
  const play = document.querySelector('#timeline-play');
  const pause = document.querySelector('#timeline-pause');
  const reset = document.querySelector('#timeline-reset');
  const status = document.querySelector('#timeline-status');
  let timer = null;
  let position = 0;
  const stop = () => {
    clearInterval(timer);
    timer = null;
    pause.disabled = true;
    play.disabled = false;
  };
  const step = () => {
    items[position].hidden = false;
    position += 1;
    status.textContent = `${position} of ${items.length} recorded events shown.`;
    if (position === items.length) {
      stop();
      play.textContent = 'Replay events';
    }
  };
  controls.hidden = false;
  play.addEventListener('click', () => {
    if (timer !== null) return;
    if (position === 0 || position === items.length) {
      position = 0;
      items.forEach(item => { item.hidden = true; });
    }
    play.disabled = true;
    pause.disabled = false;
    play.textContent = 'Continue replay';
    step();
    if (position < items.length) timer = setInterval(step, 1000);
  });
  pause.addEventListener('click', stop);
  reset.addEventListener('click', () => {
    stop();
    position = 0;
    items.forEach(item => { item.hidden = false; });
    play.textContent = 'Replay events';
    status.textContent = `All ${items.length} recorded events shown.`;
  });
}
