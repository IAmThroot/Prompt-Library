import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import test from 'node:test';

const lifecycleSource = await readFile(new URL('../web/lifecycle.js', import.meta.url), 'utf8');
const lifecycle = await import('data:text/javascript;base64,' + Buffer.from(lifecycleSource).toString('base64'));

test('retained resources preserve loaded pages and dispose once', () => {
  const resources = lifecycle.retainedResources();
  let disposed = 0;
  const gallery = resources.get(1, () => ({ pages: [1, 2], dispose() { disposed++; } }));
  resources.prune([1]);
  assert.equal(resources.get(1, () => assert.fail()), gallery);
  assert.deepEqual(gallery.pages, [1, 2]);
  resources.dispose(); resources.dispose();
  assert.equal(disposed, 1);
});

test('new requests and panel disposal cancel obsolete work', () => {
  const parent = new AbortController();
  const requests = lifecycle.latestRequest(parent.signal);
  const old = requests.next();
  const current = requests.next();
  assert.equal(old.signal.aborted, true);
  assert.equal(old.current(), false);
  assert.equal(current.current(), true);
  parent.abort();
  assert.equal(current.signal.aborted, true);
  old.finish(); current.finish();
});

test('media disposal stops playback and releases source', () => {
  const operations = [];
  lifecycle.releaseMedia({ pause() { operations.push('pause'); }, removeAttribute(k) { operations.push(k); }, load() { operations.push('load'); } });
  assert.deepEqual(operations, ['pause', 'src', 'load']);
});

// Exercise the actual panel/gallery functions using a minimal DOM and mocked HTTP transport.
class Element {
  constructor(tag) { this.tag = tag; this.nodeType = 1; this.children = []; this.listeners = {}; this.style = {}; this.dataset = {}; this.classList = { add() {}, remove() {} }; this.isConnected = true; }
  append(...children) { for (let child of children) { if (!child || typeof child !== 'object') child = { nodeType: 3, textContent: String(child) }; child.parent?.removeChild(child); child.parent = this; this.children.push(child); } }
  removeChild(child) { this.children = this.children.filter((x) => x !== child); }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  setAttribute(k, v) { this[k] = v; }
  removeAttribute(k) { delete this[k]; }
  addEventListener(k, fn) { (this.listeners[k] ||= []).push(fn); }
  remove() { this.parent?.removeChild(this); this.isConnected = false; }
  click() { for (const fn of this.listeners.click || []) fn({ target: this, stopPropagation() {} }); }
}
const text = (el) => el.textContent || el.children?.map(text).join('') || '';
const find = (el, predicate) => predicate(el) ? el : (el.children || []).map((x) => find(x, predicate)).find(Boolean);
const flush = () => new Promise((resolve) => setImmediate(resolve));
async function panelHarness(fetchApi) {
  const document = { createElement: (tag) => new Element(tag), createTextNode: (value) => ({ nodeType: 3, textContent: value }), addEventListener() {}, body: new Element('body'), head: new Element('head') };
  const context = vm.createContext({ document, AbortController, console, setTimeout, clearTimeout, setInterval, URLSearchParams, navigator: {}, confirm: () => true });
  const app = { registerExtension() {} };
  const api = { fetchApi, apiURL: (path) => path };
  const source = await readFile(new URL('../web/prompt_library.js', import.meta.url), 'utf8');
  const module = new vm.SourceTextModule(source + '\nexport { createPanel, generationGallery };', { context });
  await module.link(async (specifier) => {
    if (specifier === './lifecycle.js') return new vm.SourceTextModule(lifecycleSource, { context });
    const key = specifier.includes('app.js') ? 'app' : 'api';
    return new vm.SyntheticModule([key], function () { this.setExport(key, key === 'app' ? app : api); }, { context });
  });
  await module.evaluate();
  return module.namespace;
}
const response = (data) => ({ ok: true, json: async () => data });

test('actual panel preserves gallery pages through selection rerenders', async () => {
  let galleryCalls = 0;
  const module = await panelHarness(async (path) => {
    if (path.endsWith('/collections')) return response({ items: [] });
    if (path.includes('/list?')) return response({ total: 1, items: [{ id: 1, positive: 'test', negative: '', media: [], meta: {}, kind: 'prompt' }] });
    if (path.includes('/gallery/')) { galleryCalls++; return response({ total: 2, items: [{ id: String(galleryCalls), path: 'image.png', mtime: 1, media_type: 'image', snapshot: {}, workflows: [] }] }); }
    if (path.includes('/files/')) return response({ files: [] });
    if (path.includes('/workflows/')) return response({ items: [] });
    throw Error(path);
  });
  const panel = module.createPanel();
  await panel.reload();
  find(panel.el, (x) => x.className === 'pl-head').click();
  await flush();
  find(panel.el, (x) => x.tag === 'button' && text(x) === 'Load more generations').click();
  await flush();
  const gallery = find(panel.el, (x) => x.className === 'pl-gallery');
  assert.equal(galleryCalls, 2);
  find(panel.el, (x) => x.tag === 'button' && text(x) === 'Select page').click();
  await flush();
  assert.equal(find(panel.el, (x) => x.className === 'pl-gallery'), gallery);
  assert.equal(galleryCalls, 2);
  panel.dispose();
});

test('actual gallery disposal aborts pending fetch', async () => {
  let signal;
  const module = await panelHarness((path, options) => {
    signal = options.signal;
    return new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(Object.assign(Error('aborted'), { name: 'AbortError' }))));
  });
  const gallery = module.generationGallery(1);
  gallery.dispose();
  assert.equal(signal.aborted, true);
  await flush();
});

test('actual panel ignores stale responses and aborts on disposal', async () => {
  const pending = [];
  const module = await panelHarness((path, options) => {
    if (path.endsWith('/collections')) return Promise.resolve(response({ items: [] }));
    if (path.includes('/list?')) return new Promise((resolve) => pending.push({ resolve, signal: options.signal }));
    throw Error(path);
  });
  const panel = module.createPanel();
  const first = panel.reload(); await flush();
  const second = panel.reload(); await flush();
  assert.equal(pending[0].signal.aborted, true);
  const item = (positive) => ({ total: 1, items: [{ id: 1, positive, negative: '', media: [], meta: {}, kind: 'prompt' }] });
  pending[1].resolve(response(item('newer'))); await second;
  pending[0].resolve(response(item('stale'))); await first;
  assert.ok(find(panel.el, (x) => x.textContent === 'newer'));
  assert.equal(find(panel.el, (x) => x.textContent === 'stale'), undefined);
  const third = panel.reload(); await flush();
  panel.dispose();
  assert.equal(pending[2].signal.aborted, true);
  pending[2].resolve(response(item('disposed'))); await third;
  assert.equal(find(panel.el, (x) => x.textContent === 'disposed'), undefined);
});
