// 授权台交互：检查服务可用性，读取会话 Cookie 并导入。

const statusEl = document.getElementById('status');
const outEl = document.getElementById('out');
const button = document.getElementById('go');

function setStatus(text, klass) {
  statusEl.textContent = text;
  statusEl.className = 'status' + (klass ? ' ' + klass : '');
}

async function checkService() {
  const health = await chrome.runtime.sendMessage({ type: 'probe' });
  if (health === null || health === undefined) {
    setStatus('本机服务未启动（先运行 python -m hark_bridge）', 'bad');
    button.disabled = true;
    return;
  }
  setStatus(
    `服务正常 · 账号 ${health.accounts_total} 个，可用 ${health.accounts_dispatchable} 个`,
    'ok'
  );
  button.disabled = false;
}

button.onclick = async () => {
  const account = document.getElementById('account').value.trim();
  const label = document.getElementById('label').value.trim();
  if (!account) {
    outEl.textContent = '账号标签必填';
    return;
  }

  button.disabled = true;
  outEl.textContent = '读取 Cookie 并校验中…';

  const result = await chrome.runtime.sendMessage({
    type: 'import',
    account,
    label
  });

  outEl.textContent = JSON.stringify(result, null, 2);
  button.disabled = false;

  if (result && result.ok) {
    setStatus('导入成功', 'ok');
  } else {
    setStatus('导入失败', 'bad');
  }
};

checkService();
