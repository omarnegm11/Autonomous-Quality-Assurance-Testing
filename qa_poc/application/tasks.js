function visibleTasks(tasks) {
  return tasks;
}

const $ = id => document.getElementById(id);
let tasks = [];
async function api(path, options = {}) {
  const response = await fetch('/api' + path, {
    ...options, headers: {'Content-Type': 'application/json'}, credentials: 'same-origin'
  });
  if (!response.ok) {
    const detail = await response.json();
    throw new Error(detail.detail || `HTTP ${response.status}`);
  }
  return response.status === 204 ? null : response.json();
}
function showError(error) { $('message').textContent = error.message; }
async function refresh() {
  document.body.dataset.ready = 'false';
  try {
    tasks = await api('/tasks');
    $('login-panel').hidden = true; $('task-panel').hidden = false;
    $('task-list').replaceChildren();
    for (const task of visibleTasks(tasks)) {
      const row = document.createElement('li');
      row.dataset.taskId = task.id;
      const title = document.createElement('input');
      title.value = task.title; title.dataset.testid = `title-${task.id}`;
      title.setAttribute('aria-label', `Title for task ${task.id}`);
      const save = document.createElement('button');
      save.textContent = 'Save'; save.dataset.testid = `save-${task.id}`;
      save.onclick = async () => {
        try { await api(`/tasks/${task.id}`, {method: 'PATCH', body: JSON.stringify({title: title.value})}); await refresh(); }
        catch (e) { showError(e); }
      };
      const complete = document.createElement('button');
      complete.textContent = task.completed ? 'Reopen' : 'Complete'; complete.dataset.testid = `complete-${task.id}`;
      complete.onclick = async () => {
        try { await api(`/tasks/${task.id}`, {method: 'PATCH', body: JSON.stringify({completed: !task.completed})}); await refresh(); }
        catch (e) { showError(e); }
      };
      const remove = document.createElement('button');
      remove.textContent = 'Delete'; remove.dataset.testid = `delete-${task.id}`;
      remove.onclick = async () => {
        try { await api(`/tasks/${task.id}`, {method: 'DELETE'}); await refresh(); }
        catch (e) { showError(e); }
      };
      row.append(title, save, complete, remove); $('task-list').append(row);
    }
    document.body.dataset.ready = 'true';
  } catch (e) {
    $('login-panel').hidden = false; $('task-panel').hidden = true;
    document.body.dataset.ready = 'true';
  }
}
$('login-form').onsubmit = async event => {
  event.preventDefault(); $('message').textContent = '';
  try {
    await api('/login', {method: 'POST', body: JSON.stringify({username: $('username').value, password: $('password').value})});
    $('password').value = ''; await refresh();
  } catch (e) { $('password').value = ''; showError(e); }
};
$('task-form').onsubmit = async event => {
  event.preventDefault(); $('message').textContent = '';
  try {
    const task = await api('/tasks', {method: 'POST', body: JSON.stringify({title: $('new-title').value})});
    $('new-title').value = ''; $('last-created').textContent = String(task.id);
    await refresh();
  } catch (e) { showError(e); }
};
$('logout').onclick = async () => {
  try { await api('/logout', {method: 'POST'}); await refresh(); } catch (e) { showError(e); }
};
refresh();
