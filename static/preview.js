// Static illustrations only. Service integration must replace this file, not reuse it for payments.
const review = document.querySelector('[data-preview-review]');
const confirmation = document.querySelector('#confirmation');
const message = document.querySelector('#message');
review?.addEventListener('click', () => {
  confirmation.hidden = false;
  document.querySelector('#confirmation-status').textContent = '';
  confirmation.focus();
});
document.querySelector('[data-edit-message]')?.addEventListener('click', () => {
  confirmation.hidden = true;
  message.focus();
});
message?.addEventListener('input', () => { confirmation.hidden = true; });
document.querySelector('[data-preview-confirm]')?.addEventListener('click', () => {
  document.querySelector('#confirmation-status').textContent = 'Example confirmed. Nothing was saved or sent; no refund was requested.';
});
const approval = document.querySelector('[data-preview-approval]');
const illustration = document.querySelector('#approval-preview');
approval?.addEventListener('click', () => {
  illustration.hidden = false;
  illustration.focus();
});
document.querySelector('[data-close-approval]')?.addEventListener('click', () => {
  illustration.hidden = true;
  approval.focus();
});
