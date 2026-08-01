const $ = selector => document.querySelector(selector);
const state = {query: '', cards: []};
if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js');
const endpoint = $('#endpoint');
const token = $('#token');
endpoint.value = localStorage.getItem('recallEndpoint') || '';
token.value = localStorage.getItem('recallToken') || '';

$('#save').addEventListener('click', () => {
  localStorage.setItem('recallEndpoint', endpoint.value.trim());
  localStorage.setItem('recallToken', token.value);
  setStatus('接続設定をこの端末に保存しました。');
});

$('#search-form').addEventListener('submit', async event => {
  event.preventDefault();
  state.query = $('#query').value.trim();
  $('#synthesis').hidden = true;
  $('#results').replaceChildren();
  try {
    setStatus('過去の記録を探しています…');
    const result = await relay({type: 'search', query: state.query});
    if (result.error) throw new Error(result.error);
    state.cards = result.cards || [];
    renderCards();
    setStatus(state.cards.length ? `${state.cards.length}件見つかりました。` : '一致する記録はありません。');
  } catch (error) { setStatus(error.message, true); }
});

async function relay(payload) {
  if (!endpoint.value || !token.value) throw new Error('先に接続設定を保存してください。');
  const response = await fetch(endpoint.value, {method: 'POST', body: JSON.stringify({
    mode: 'ask', token: token.value, ...payload
  })});
  const asked = await response.json();
  if (!asked.id) throw new Error(asked.error || '質問を送信できませんでした。');
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 2000));
    const answer = await jsonp({mode: 'answer', id: asked.id, token: token.value});
    if (answer.status === 'done') return answer.result;
  }
  throw new Error('30秒以内に応答がありませんでした。');
}

function jsonp(params) {
  return new Promise((resolve, reject) => {
    const callback = `recall_${Date.now()}_${Math.random().toString(16).slice(2)}`;
    const script = document.createElement('script');
    const timer = setTimeout(() => finish(new Error('中継への接続がタイムアウトしました。')), 10000);
    const finish = error => { clearTimeout(timer); delete window[callback]; script.remove(); error ? reject(error) : null; };
    window[callback] = value => { finish(); resolve(value); };
    script.onerror = () => finish(new Error('中継に接続できませんでした。'));
    script.src = `${endpoint.value}?${new URLSearchParams({...params, callback})}`;
    document.head.append(script);
  });
}

function renderCards() {
  const root = $('#results');
  state.cards.forEach((card, index) => {
    const article = document.createElement('article'); article.className = 'card';
    const meta = element('p', card.date || '日付不明', 'meta');
    const title = element('h2', card.title);
    const summary = element('p', card.summary || '要約はまだありません。', 'summary');
    const actions = document.createElement('div'); actions.className = 'actions';
    const label = document.createElement('label');
    const check = document.createElement('input'); check.type = 'checkbox'; check.checked = true; check.dataset.id = card.id;
    label.append(check, ' まとめに含める');
    const open = document.createElement('a'); open.className = 'open'; open.href = card.link; open.textContent = 'Obsidianで開く';
    actions.append(label, open); article.append(meta, title, summary, actions); root.append(article);
  });
  if (state.cards.length) {
    const button = element('button', `この${state.cards.length}件をまとめる`, 'summarize');
    button.addEventListener('click', summarize); root.append(button);
  }
}

async function summarize() {
  const ids = [...document.querySelectorAll('[data-id]:checked')].map(input => input.dataset.id);
  if (!ids.length) return setStatus('まとめるカードを1件以上選んでください。', true);
  try {
    setStatus('選んだ記録をまとめています…');
    const result = await relay({type: 'summarize', query: state.query, ids});
    if (!result.answer) throw new Error(result.error || 'まとめを作成できませんでした。');
    $('#answer').textContent = result.answer; $('#synthesis').hidden = false; setStatus('まとめができました。');
  } catch (error) { setStatus(error.message, true); }
}

function element(tag, text, className) { const node = document.createElement(tag); node.textContent = text; if (className) node.className = className; return node; }
function setStatus(message, error = false) { $('#status').textContent = message; $('#status').style.color = error ? '#a33b32' : ''; }
