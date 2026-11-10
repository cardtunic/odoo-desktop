const invoke = window.__TAURI__.core.invoke;
const status = document.getElementById('status');
let pending = false;
let polling = false;
let settings = {};

function message(text, error = false) {
  status.textContent = text;
  status.dataset.error = String(error);
}
function busy(value, action = '') {
  pending = value;
  document.querySelectorAll('input:not(#signin-url), button').forEach(element => { element.disabled = value; });
  document.getElementById('cancel').hidden = !(value && action === 'connect-drive');
  document.getElementById('cancel').disabled = false;
  if (!value) {
    document.getElementById('connect-drive').disabled = !settings.client_configured || settings.connected;
    document.getElementById('disconnect-drive').disabled = !settings.connected;
    document.getElementById('drive-backup').disabled = !settings.connected;
    document.getElementById('configure').disabled = settings.connected;
    document.getElementById('zip-import').disabled = !settings.archive;
  }
}
async function refresh() {
  settings = await invoke('backup_settings');
  document.getElementById('google-status').textContent = settings.connected
    ? 'Google Drive connection saved. Only files created by this app are accessible.'
    : settings.client_configured ? 'Google client configured. Sign in to connect Drive.' : 'Choose your Google OAuth Desktop app client JSON to get started.';
  document.getElementById('local-database').textContent = settings.local_database;
  document.getElementById('selected').textContent = settings.archive || 'No ZIP selected.';
}
async function poll() {
  if (polling) return;
  polling = true;
  try {
    const state = await invoke('backup_status');
    message(state.message + (state.active && state.done ? '\nFinishing operation and restarting Odoo if needed…' : ''), state.error);
    if (!state.active) await refresh();
    busy(state.active, state.action);
    const signInUrl = state.active && state.action === 'connect-drive' ? state.auth_url : '';
    document.getElementById('signin-fallback').hidden = !signInUrl;
    if (document.getElementById('signin-url').value !== (signInUrl || '')) document.getElementById('signin-url').value = signInUrl || '';
    document.getElementById('signin-url').disabled = false;
    if (state.active) setTimeout(poll, 700);
  } catch (error) { message(String(error), true); busy(false); }
  finally { polling = false; }
}
async function run(action) {
  if (pending) return;
  try {
    busy(true, action);
    message(action === 'connect-drive' ? 'Opening Google sign-in…' : 'Starting backup operation…');
    await invoke('run_backup', { action, confirmation: document.getElementById('confirmation').value });
    await poll();
  } catch (error) { message(String(error), true); busy(false); }
}
document.getElementById('configure').addEventListener('click', async () => {
  try {
    if (await invoke('configure_google')) {
      await refresh(); busy(false);
      message('Google client saved privately. You can now sign in.');
    }
  } catch (error) { message(String(error), true); }
});
document.getElementById('choose').addEventListener('click', async () => {
  try { await invoke('choose_backup_zip'); await refresh(); busy(false); }
  catch (error) { message(String(error), true); }
});
document.getElementById('cancel').addEventListener('click', async () => {
  try { await invoke('cancel_google_signin'); message('Cancelling sign-in…'); }
  catch (error) { message(String(error), true); }
});
document.getElementById('signin-url').addEventListener('click', event => event.target.select());
for (const action of ['connect-drive', 'disconnect-drive', 'drive-backup', 'zip-import']) {
  document.getElementById(action).addEventListener('click', () => run(action));
}
(async () => {
  try { await refresh(); await poll(); }
  catch (error) { message(String(error), true); }
})();
