/** Recall PWA relay. Set KH_RECALL_TOKEN in Apps Script Properties before deploy. */
const REQUESTS_SHEET = 'requests';
const ANSWERS_SHEET = 'answers';
const MAX_AGE_MS = 24 * 60 * 60 * 1000;

function doGet(e) {
  const p = e.parameter || {};
  if (!validToken_(p.token)) return text_('');
  if (p.mode === 'pending') return json_(takePending_());
  if (p.mode === 'answer') return jsonp_(answer_(p.id), p.callback);
  return json_({error: 'unknown mode'});
}

function doPost(e) {
  let body;
  try { body = JSON.parse(e.postData.contents); } catch (_) { return json_({error: 'invalid json'}); }
  if (!validToken_(body.token)) return text_('');
  if (body.mode === 'ask') return json_(ask_(body));
  if (body.mode === 'result') return json_(saveResult_(body));
  return json_({error: 'unknown mode'});
}

function ask_(body) {
  if (!body.query || !['search', 'summarize'].includes(body.type)) return {error: 'invalid request'};
  const id = Utilities.getUuid();
  sheet_(REQUESTS_SHEET, ['id', 'timestamp', 'status', 'payload'])
    .appendRow([id, new Date().toISOString(), 'pending', JSON.stringify({
      type: body.type, query: String(body.query), ids: Array.isArray(body.ids) ? body.ids : []
    })]);
  return {id: id};
}

function takePending_() {
  const lock = LockService.getScriptLock();
  lock.waitLock(5000);
  try {
    const sheet = sheet_(REQUESTS_SHEET, ['id', 'timestamp', 'status', 'payload']);
    const rows = sheet.getDataRange().getValues();
    for (let i = 1; i < rows.length; i++) {
      if (rows[i][2] !== 'pending') continue;
      sheet.getRange(i + 1, 3).setValue('taken');
      return {request: {id: rows[i][0], payload: JSON.parse(rows[i][3])}};
    }
    return {request: null};
  } finally { lock.releaseLock(); }
}

function saveResult_(body) {
  if (!body.id || typeof body.result !== 'object') return {error: 'invalid result'};
  sheet_(ANSWERS_SHEET, ['id', 'timestamp', 'result'])
    .appendRow([String(body.id), new Date().toISOString(), JSON.stringify(body.result)]);
  return {ok: true};
}

function answer_(id) {
  if (!id) return {status: 'missing'};
  const rows = sheet_(ANSWERS_SHEET, ['id', 'timestamp', 'result']).getDataRange().getValues();
  for (let i = rows.length - 1; i >= 1; i--) {
    if (String(rows[i][0]) === String(id)) return {status: 'done', result: JSON.parse(rows[i][2])};
  }
  return {status: 'pending'};
}

function cleanupRecallRows() {
  [REQUESTS_SHEET, ANSWERS_SHEET].forEach(name => {
    const sheet = sheet_(name, name === REQUESTS_SHEET
      ? ['id', 'timestamp', 'status', 'payload'] : ['id', 'timestamp', 'result']);
    const rows = sheet.getDataRange().getValues();
    for (let i = rows.length - 1; i >= 1; i--) {
      if (Date.now() - new Date(rows[i][1]).getTime() > MAX_AGE_MS) sheet.deleteRow(i + 1);
    }
  });
}

function installCleanupTrigger() {
  ScriptApp.getProjectTriggers().filter(t => t.getHandlerFunction() === 'cleanupRecallRows')
    .forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('cleanupRecallRows').timeBased().everyHours(1).create();
}

function sheet_(name, headers) {
  const book = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = book.getSheetByName(name);
  if (!sheet) { sheet = book.insertSheet(name); sheet.appendRow(headers); }
  return sheet;
}

function validToken_(token) {
  const expected = PropertiesService.getScriptProperties().getProperty('KH_RECALL_TOKEN');
  return expected && token && String(token) === expected;
}

function json_(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON);
}

function jsonp_(value, callback) {
  if (!callback) return json_(value);
  const safe = String(callback).replace(/[^A-Za-z0-9_.$]/g, '');
  return ContentService.createTextOutput(`${safe}(${JSON.stringify(value)})`)
    .setMimeType(ContentService.MimeType.JAVASCRIPT);
}

function text_(value) { return ContentService.createTextOutput(value); }
