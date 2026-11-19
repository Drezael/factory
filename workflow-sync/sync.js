'use strict';

// Factory workflow importer: runs INSIDE the n8n image, without Docker socket,
// public API access or model API keys. Only import changed JSON files.
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const http = require('node:http');
const https = require('node:https');
const { spawn } = require('node:child_process');

const source = process.env.FACTORY_WORKFLOW_SOURCE_DIR || '/home/node/.n8n-files/workflows';
const stateDir = process.env.FACTORY_WORKFLOW_STATE_DIR || '/home/node/.n8n';
const manifestPath = path.join(stateDir, '.factory-workflow-sync-state.json');
const projectId = process.env.FACTORY_N8N_IMPORT_PROJECT_ID || 'owner';
const enabled = !['false', '0', 'no'].includes((process.env.FACTORY_N8N_AUTO_IMPORT || 'true').toLowerCase());
const autoPublishGithub = ['true', '1', 'yes'].includes((process.env.FACTORY_GITHUB_AUTO_PUBLISH || 'false').toLowerCase());
const autoPublishLimits = ['true', '1', 'yes'].includes((process.env.FACTORY_LIMIT_MONITOR_ENABLED || 'false').toLowerCase());
const limitsWorkflowId = '8520c5b0-1771-5589-ae3e-6d2d00516df1';
const githubPollerWorkflowId = 'b4f94100-6cd8-5e36-aad5-69f618e8f6bb';
const githubRunnerWorkflowId = 'c3484f60-1e80-578c-aaab-7f5c1e25be5a';
const githubMergeWorkflowId = 'f5e036f1-7ebc-5316-8c14-5ec219384c3f';
const autorunEnabled = ['true', '1', 'yes'].includes((process.env.FACTORY_GITHUB_AUTORUN_ENABLED || 'false').toLowerCase());
const automergeEnabled = ['true','1','yes'].includes((process.env.FACTORY_GITHUB_AUTOMERGE_ENABLED || 'false').toLowerCase());
function shouldPublish(id) { return (autoPublishLimits && id === limitsWorkflowId) ||
  (autoPublishGithub && (id === githubPollerWorkflowId || (autorunEnabled && id === githubRunnerWorkflowId) || (autorunEnabled && automergeEnabled && id === githubMergeWorkflowId))); }
const intervalSec = Number(process.env.FACTORY_N8N_SYNC_INTERVAL_SEC || '20');
const healthUrl = process.env.FACTORY_N8N_SYNC_HEALTH_URL || 'http://n8n:5678/healthz/readiness';
const importTimeoutSec = Number(process.env.FACTORY_N8N_SYNC_IMPORT_TIMEOUT_SEC || '180');
let stopping = false;
let child = null;
let interruptDelay = null;
const info = (...parts) => console.log(new Date().toISOString(), '[workflow-sync]', ...parts);
const warn = (...parts) => console.error(new Date().toISOString(), '[workflow-sync]', ...parts);

function validateSettings() {
  if (!Number.isInteger(intervalSec) || intervalSec < 5 || intervalSec > 3600) {
    throw new Error('FACTORY_N8N_SYNC_INTERVAL_SEC muss zwischen 5 und 3600 liegen');
  }
  if (!Number.isInteger(importTimeoutSec) || importTimeoutSec < 20 || importTimeoutSec > 1800) {
    throw new Error('FACTORY_N8N_SYNC_IMPORT_TIMEOUT_SEC muss zwischen 20 und 1800 liegen');
  }
  if (!/^owner$|^[a-zA-Z0-9_-]+$/.test(projectId)) {
    throw new Error('Ungueltige FACTORY_N8N_IMPORT_PROJECT_ID');
  }
  if (!process.env.N8N_ENCRYPTION_KEY) {
    throw new Error('N8N_ENCRYPTION_KEY fehlt; Importer darf keinen eigenen Schluessel generieren');
  }
  const parsed = new URL(healthUrl);
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error('FACTORY_N8N_SYNC_HEALTH_URL muss HTTP(S) sein');
  }
}

function delay(ms) {
  return new Promise((resolve) => {
    const t = setTimeout(() => { interruptDelay = null; resolve(); }, ms);
    interruptDelay = () => { clearTimeout(t); interruptDelay = null; resolve(); };
  });
}

function checkHealth() {
  return new Promise((resolve) => {
    const parsed = new URL(healthUrl);
    const transport = parsed.protocol === 'https:' ? https : http;
    const req = transport.get(parsed, { timeout: 4000 }, (res) => {
      res.resume();
      resolve(res.statusCode >= 200 && res.statusCode < 300);
    });
    req.on('timeout', () => req.destroy(new Error('timeout')));
    req.on('error', () => resolve(false));
  });
}

async function readManifest() {
  try {
    const content = JSON.parse(await fs.readFile(manifestPath, 'utf8'));
    if (content.version === 1 && content.projectId === projectId && content.files && typeof content.files === 'object') {
      return content;
    }
    info('Projektzuordnung oder State-Format geaendert: alle Dateien werden neu synchronisiert');
  } catch (err) {
    if (err.code !== 'ENOENT') warn('State nicht lesbar; starte neuen Import-Zyklus:', err.message);
  }
  return { version: 1, projectId, files: {} };
}

async function writeManifest(state) {
  const tmp = `${manifestPath}.${process.pid}.tmp`;
  await fs.writeFile(tmp, JSON.stringify(state, null, 2) + '\n', { mode: 0o600 });
  await fs.rename(tmp, manifestPath);
}

function validateWorkflow(fileName, data) {
  if (!data || typeof data !== 'object' || !data.id || typeof data.id !== 'string' ||
      !data.name || typeof data.name !== 'string' || !Array.isArray(data.nodes) ||
      !data.connections || typeof data.connections !== 'object') {
    throw new Error(`Ungueltige Workflow-Definition: ${fileName}`);
  }
  if (data.active === true) {
    throw new Error(`Workflow muss im JSON inaktiv sein (active=false): ${fileName}`);
  }
}

async function workflowFiles() {
  const names = (await fs.readdir(source)).filter((n) => n.endsWith('.json')).sort();
  const seenIds = new Set();
  const result = [];
  for (const name of names) {
    const fullPath = path.join(source, name);
    const stat = await fs.lstat(fullPath);
    if (!stat.isFile()) throw new Error(`Nur regulaere JSON-Dateien erlaubt: ${name}`);
    const body = await fs.readFile(fullPath);
    const json = JSON.parse(body.toString('utf8'));
    validateWorkflow(name, json);
    if (seenIds.has(json.id)) throw new Error(`Doppelte Workflow-ID: ${json.id}`);
    seenIds.add(json.id);
    result.push({ name, fullPath, id: json.id, digest: crypto.createHash('sha256').update(body).digest('hex') });
  }
  return result;
}

async function runImport(file) {
  const args = ['import:workflow', `--input=${file.fullPath}`];
  if (projectId !== 'owner') args.push(`--projectId=${projectId}`);
  info('Import:', file.name);
  return new Promise((resolve) => {
    let finished = false;
    let output = '';
    const append = (chunk) => {
      output = (output + chunk.toString('utf8')).slice(-12000);
    };
    child = spawn('n8n', args, { stdio: ['ignore', 'pipe', 'pipe'] });
    const timer = setTimeout(() => {
      warn('Import-Timeout:', file.name);
      if (child) child.kill('SIGTERM');
    }, importTimeoutSec * 1000);
    const done = (okay, reason) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      child = null;
      if (!okay) warn('Import fehlgeschlagen:', file.name, reason, output.trim());
      resolve(okay);
    };
    child.stdout.on('data', append);
    child.stderr.on('data', append);
    child.on('error', (err) => done(false, err.message));
    child.on('close', (code, signal) => done(code === 0, `exit=${code}, signal=${signal || 'none'}`));
  });
}

async function publishGithubWorkflow(id) {
  if (!shouldPublish(id)) return true;
  info('Publish GitHub polling workflow:', id);
  return new Promise((resolve) => {
    let finished = false;
    let output = '';
    child = spawn('n8n', ['publish:workflow', `--id=${id}`], { stdio: ['ignore', 'pipe', 'pipe'] });
    const timer = setTimeout(() => {
      warn('Publish timeout:', id);
      if (child) child.kill('SIGTERM');
    }, importTimeoutSec * 1000);
    const done = (okay, reason) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      child = null;
      if (!okay) warn('Publish failed:', id, reason, output.trim());
      resolve(okay);
    };
    child.stdout.on('data', (data) => { output = (output + data.toString('utf8')).slice(-10000); });
    child.stderr.on('data', (data) => { output = (output + data.toString('utf8')).slice(-10000); });
    child.on('error', (err) => done(false, err.message));
    child.on('close', (code, signal) => done(code === 0, `exit=${code}, signal=${signal || 'none'}`));
  });
}

async function syncOnce() {
  if (!enabled || stopping) return;
  if (!await checkHealth()) {
    info('n8n noch nicht erreichbar; warte auf den naechsten Durchlauf');
    return;
  }
  const files = await workflowFiles(); // Validate ALL files before touching the DB.
  if (!files.length) {
    info('Keine Workflow-JSON-Dateien vorhanden');
    return;
  }
  const state = await readManifest();
  let changed = 0;
  let failed = 0;
  for (const file of files) {
    if (stopping) return;
    const old = state.files[file.name];
    if (old && old.digest === file.digest && old.id === file.id &&
        !(shouldPublish(file.id) && !old.published)) continue;
    changed++;
    const success = await runImport(file);
    if (success) {
      const published = await publishGithubWorkflow(file.id);
      if (published) {
        state.files[file.name] = { id: file.id, digest: file.digest,
                                   published: shouldPublish(file.id) };
        await writeManifest(state); // Per-file durable progress.
        info('Import erfolgreich:', file.name);
      } else {
        failed++;
      }
    } else {
      failed++;
    }
  }
  if (!changed) info('Workflow-Dateien unveraendert: kein Import erforderlich');
  else info('Zyklus abgeschlossen:', changed - failed, 'importiert,', failed, 'fehlgeschlagen');
}

async function main() {
  validateSettings();
  await fs.mkdir(stateDir, { recursive: true });
  info(`Start: enabled=${enabled}, interval=${intervalSec}s, project=${projectId}`);
  if (!enabled) info('Automatischer Import in .env deaktiviert');
  while (!stopping) {
    try {
      await syncOnce();
    } catch (err) {
      warn('Sync-Fehler:', err.stack || err.message);
    }
    if (!stopping) await delay(intervalSec * 1000);
  }
}

function shutdown() {
  stopping = true;
  if (child) child.kill('SIGTERM');
  if (interruptDelay) interruptDelay();
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
main().catch((err) => { warn('Fatal:', err.stack || err.message); process.exitCode = 1; });
