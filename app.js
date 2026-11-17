const $ = id => document.getElementById(id);
const fields = ['preset', 'sender', 'name', 'username', 'host', 'port', 'security', 'delay', 'recipients', 'subject', 'message'];
let dupTimer, pending = null, running = false, saved = {}, ready = false, saveTimer, saveChain = Promise.resolve();
const OFFLINE = 'Cannot reach the local app. Keep the Python window running.';
const addresses = () => [...new Set($('recipients').value.split(/[,;\s]+/).map(x => x.trim().toLowerCase()).filter(Boolean))];

async function api(path, data) {
  const response = await fetch('/api/' + path, {
    method: data ? 'POST' : 'GET',
    headers: {'X-App-Token': token, 'Content-Type': 'application/json'},
    body: data ? JSON.stringify(data) : undefined
  });
  const result = await response.json();
  if (!response.ok) throw Error(result.error || 'Request failed');
  return result;
}

function snapshot() {
  const data = Object.fromEntries(fields.map(id => [id, $(id).value]));
  data.password = $('password').value;
  data.remember_password = $('remember').checked;
  return data;
}

function updateSaved() {
  const matches = ['sender', 'username', 'host'].every(id => $(id).value.toLowerCase() === (saved[id] || '').toLowerCase());
  const available = saved.has_password && matches;
  $('password').required = !available;
  $('password').placeholder = available ? 'Saved securely · leave blank to reuse' : 'Enter your Gmail App Password';
  $('resume').required = !saved.filename;
  $('savedResume').textContent = saved.filename ? `Saved resume: ${saved.filename}. Choose a file to replace it.` : 'No saved resume yet. PDF recommended · Maximum 10 MB';
  $('count').textContent = `· ${addresses().length} unique`;
}

async function fileData() {
  const file = $('resume').files[0];
  if (!file) return {};
  if (file.size > 10 * 1024 * 1024 || !file.size) throw Error('Resume must be between 1 byte and 10 MB.');
  const attachment = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(',')[1]);
    reader.onerror = () => reject(Error('Could not read the resume.'));
    reader.readAsDataURL(file);
  });
  return {attachment, filename: file.name};
}

function saveNow() {
  clearTimeout(saveTimer);
  const data = snapshot();
  const chosen = $('resume').files[0];
  const file = fileData();
  // Serialize saves so a slow older request cannot replace newer form contents.
  const operation = saveChain.catch(() => {}).then(async () => {
    $('saveStatus').textContent = 'Saving…';
    const result = await api('settings', {...data, ...await file});
    saved = result;
    if (chosen && $('resume').files[0] === chosen) $('resume').value = '';
    $('saveStatus').textContent = 'Saved locally';
    updateSaved();
    return result;
  });
  saveChain = operation;
  return operation;
}

function scheduleSave() {
  if (!ready) return;
  updateSaved();
  $('saveStatus').textContent = 'Unsaved changes…';
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => saveNow().catch(showSaveError), 700);
}

function showSaveError(error) {
  $('saveStatus').textContent = `Not saved: ${error.message}`;
}

$('form').addEventListener('input', scheduleSave);
$('form').addEventListener('change', scheduleSave);
$('sender').onchange = () => {
  if (!$('username').value || $('username').value === (saved.sender || '')) $('username').value = $('sender').value;
};
$('preset').onchange = () => {
  if ($('preset').value === 'gmail') {
    $('host').value = 'smtp.gmail.com'; $('port').value = 587; $('security').value = 'starttls';
  }
};
$('security').onchange = () => { $('port').value = $('security').value === 'ssl' ? 465 : 587; };

$('form').onsubmit = async event => {
  event.preventDefault();
  $('notice').textContent = '';
  $('review').disabled = true;
  try {
    await saveNow();
    pending = {...snapshot(), ...await fileData()};
    const history = await api('history');
    const blocked = new Set(history.items.map(item => item.email));
    const recipients = addresses();
    const skipped = recipients.filter(email => blocked.has(email)).length;
    $('previewMeta').textContent = `From: ${pending.sender}\nRecipients: ${recipients.length} · ${recipients.length - skipped} eligible · ${skipped} will be skipped\nResume: ${pending.filename || saved.filename}\nSubject: ${pending.subject}`;
    $('previewBody').textContent = pending.message;
    $('confirm').disabled = recipients.length === skipped;
    $('preview').showModal();
  } catch (error) { $('notice').textContent = error.message; }
  finally { $('review').disabled = running || !ready; }
};
$('cancel').onclick = () => { pending = null; $('preview').close(); };
$('preview').addEventListener('cancel', () => { pending = null; });
$('confirm').onclick = async () => {
  $('confirm').disabled = true;
  try {
    await api('send', pending);
    pending = null;
    $('password').value = '';
    saved = await api('settings');
    updateSaved();
    $('preview').close();
    await refresh();
  } catch (error) { $('preview').close(); $('notice').textContent = error.message; }
  finally { $('confirm').disabled = false; }
};
$('stop').onclick = async () => {
  try { await api('stop', {}); $('notice').textContent = 'Stop requested. The email already in progress will finish.'; }
  catch (error) { $('notice').textContent = error.message; }
};

async function refresh() {
  try {
    const state = await api('status');
    running = state.running;
    if ($('notice').textContent === OFFLINE) $('notice').textContent = '';
    if (state.error) $('notice').textContent = state.error;
    $('review').disabled = running || !ready;
    $('stop').disabled = !running;
    const tally = status => state.items.filter(x => x.status === status).length;
    $('summary').textContent = `${running ? 'Sending' : 'Queue idle'} · ${tally('Sent')} sent · ${tally('Failed')} failed · ${tally('Uncertain')} uncertain · ${tally('Skipped')} skipped · ${tally('Queued')} queued · ${tally('Stopped')} stopped`;
    const scroll = $('items').scrollTop;
    $('items').replaceChildren(...state.items.map(item => {
      const div = document.createElement('div'); div.className = 'item';
      const email = document.createElement('span'); email.textContent = item.email;
      const status = document.createElement('span'); status.textContent = item.status; status.className = item.status;
      div.append(email, status);
      if (item.detail) { const detail = document.createElement('small'); detail.textContent = item.detail; div.append(detail); }
      return div;
    }));
    $('items').scrollTop = scroll;
  } catch (error) { $('notice').textContent = OFFLINE; }
}

async function checkDuplicates() {
  try {
    const history = await api('history');
    const blocked = new Map(history.items.map(item => [item.email, item]));
    const hits = addresses().filter(email => blocked.has(email));
    $('dupWarning').textContent = hits.length
      ? 'Already emailed within 30 days (will be skipped):\n' + hits.map(email => {
          const item = blocked.get(email);
          return `${email} · ${item.status === 'Sent' ? 'sent' : 'uncertain attempt, check Gmail Sent'} ${new Date(item.sent_at * 1000).toLocaleDateString()} · eligible again ${new Date(item.eligible_at * 1000).toLocaleDateString()}`;
        }).join('\n')
      : '';
  } catch (error) { $('dupWarning').textContent = ''; }
}

async function initialize() {
  try {
    saved = await api('settings');
    for (const id of fields) if (saved[id] !== undefined) $(id).value = saved[id];
    $('remember').checked = saved.has_password || !saved.sender;
    $('databasePath').textContent = `Local database: ${saved.database}`;
    updateSaved();
    ready = true;
    $('saveStatus').textContent = saved.sender ? 'Saved details restored' : 'Details will save automatically as you edit';
    $('savedFields').disabled = false;
  } catch (error) { $('saveStatus').textContent = `Cannot load saved data: ${error.message}. Reload the app before editing.`; }
  await refresh(); await checkDuplicates();
}

setInterval(refresh, 1500);
setInterval(checkDuplicates, 5000);
$('recipients').addEventListener('input', () => { clearTimeout(dupTimer); dupTimer = setTimeout(checkDuplicates, 400); });
initialize();
window.addEventListener('beforeunload', event => {
  if (running || /^(Unsaved|Saving)/.test($('saveStatus').textContent)) {
    event.preventDefault(); event.returnValue = '';
  }
});
