'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'web/motion.js'), 'utf8');

class Element {
  constructor(document, attributes = {}, text = '') {
    this.ownerDocument = document;
    this.attributes = new Map(Object.entries(attributes));
    this.parentElement = null;
    this.children = [];
    this._text = text;
    this.classes = new Set();
    this.classList = {
      add: (...names) => names.forEach(name => this.classes.add(name)),
      remove: (...names) => names.forEach(name => this.classes.delete(name)),
      contains: name => this.classes.has(name),
    };
    this.styles = new Map();
    this.style = {
      setProperty: (key, value) => this.styles.set(key, value),
      removeProperty: key => this.styles.delete(key),
    };
  }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  get textContent() { return this.children.length ? this.children.map(child => child.textContent).join('') : this._text; }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  append(child) { child.parentElement = this; this.children.push(child); return child; }
  replaceChildren(...children) {
    for (const child of this.children) child.parentElement = null;
    this.children = [];
    this._text = '';
    children.forEach(child => this.append(child));
  }
  matches(selector) {
    const match = /^\[([^\]]+)\]$/.exec(selector);
    return Boolean(match && this.attributes.has(match[1]));
  }
  querySelectorAll(selector) {
    return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
}

function fixture({ reduced = false, observer = true, rotate = true } = {}) {
  const listeners = new Map();
  const mediaListeners = new Set();
  const document = {
    hidden: false,
    visibilityState: 'visible',
    createElement: () => new Element(document),
    addEventListener(name, callback) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(callback);
    },
    removeEventListener(name, callback) { listeners.get(name)?.delete(callback); },
  };
  const media = {
    matches: reduced,
    addEventListener: (_, callback) => mediaListeners.add(callback),
    removeEventListener: (_, callback) => mediaListeners.delete(callback),
  };
  const root = new Element(document);
  const reveal = root.append(new Element(document, { 'data-reveal': '' }, 'Содержимое'));
  const countLabel = root.append(new Element(document, { 'aria-label': '2 учебных источника' }));
  const count = countLabel.append(new Element(document, { 'data-count': '2', 'aria-hidden': 'true' }, '2'));
  let rotator;
  if (rotate) {
    const label = root.append(new Element(document, { 'aria-label': 'Поиск совпадений, пересказов и переводов' }));
    rotator = label.append(new Element(document, {
      'data-rotate': '["совпадения","пересказы","переводы"]', 'aria-hidden': 'true',
    }, 'совпадения'));
  }
  let identifier = 0;
  const frames = new Map();
  const intervals = new Map();
  const timeouts = new Map();
  const observers = [];
  class Observer {
    constructor(callback) {
      this.callback = callback;
      this.observed = new Set();
      this.disconnected = false;
      observers.push(this);
    }
    observe(element) { this.observed.add(element); }
    unobserve(element) { this.observed.delete(element); }
    disconnect() { this.disconnected = true; this.observed.clear(); }
    trigger(...elements) { this.callback(elements.map(target => ({ target, isIntersecting: true }))); }
  }
  const window = {
    matchMedia: () => media,
    requestAnimationFrame: callback => { frames.set(++identifier, callback); return identifier; },
    cancelAnimationFrame: id => frames.delete(id),
    setInterval: callback => { intervals.set(++identifier, callback); return identifier; },
    clearInterval: id => intervals.delete(id),
    setTimeout: callback => { timeouts.set(++identifier, callback); return identifier; },
    clearTimeout: id => timeouts.delete(id),
    fetch: () => { throw new Error('Motion must never use the network'); },
  };
  if (observer) window.IntersectionObserver = Observer;
  vm.runInNewContext(source, { window, document, console });
  return {
    window, root, reveal, count, countLabel, rotator, document, frames, intervals, timeouts,
    observers, listeners, mediaListeners,
    frame(time) {
      const callbacks = [...frames.values()]; frames.clear();
      callbacks.forEach(callback => callback(time));
    },
    interval() { [...intervals.values()].forEach(callback => callback()); },
    timeout() {
      const callbacks = [...timeouts.values()]; timeouts.clear();
      callbacks.forEach(callback => callback());
    },
    visibility(hidden) {
      document.hidden = hidden;
      document.visibilityState = hidden ? 'hidden' : 'visible';
      [...listeners.get('visibilitychange') || []].forEach(callback => callback());
    },
    reduce(matches) { media.matches = matches; [...mediaListeners].forEach(callback => callback()); },
  };
}

test('static content stays visible without IntersectionObserver and no counter RAF starts', () => {
  const f = fixture({ observer: false, rotate: false });
  assert.equal(f.count.textContent, '2');
  assert.equal(f.reveal.classes.size, 0);
  f.window.IstokMotion.mount(f.root);
  assert.equal(f.reveal.classList.contains('istok-motion-pending'), false);
  assert.equal(f.count.textContent, '2');
  assert.equal(f.frames.size, 0);
  f.window.IstokMotion.teardown();
});

test('reveal and labelled hidden counter animate only once when entering view', () => {
  const f = fixture({ rotate: false });
  f.window.IstokMotion.mount(f.root);
  const io = f.observers[0];
  assert.ok(f.reveal.classList.contains('istok-motion-pending'));
  assert.equal(f.count.textContent, '2');
  io.trigger(f.reveal, f.count);
  assert.equal(f.reveal.classList.contains('istok-motion-pending'), false);
  assert.equal(f.count.textContent, '0');
  assert.equal(io.observed.has(f.count), false);
  f.frame(0);
  f.frame(760);
  assert.equal(f.count.textContent, '2');
  assert.equal(f.frames.size, 0);
  io.trigger(f.count);
  assert.equal(f.count.textContent, '2');
  assert.equal(f.frames.size, 0);
  assert.equal(f.countLabel.getAttribute('aria-label'), '2 учебных источника');
  f.window.IstokMotion.teardown();
});

test('reduced motion keeps final counts and static first phrase without observers or timers', () => {
  const f = fixture({ reduced: true });
  f.window.IstokMotion.mount(f.root);
  assert.equal(f.count.textContent, '2');
  assert.equal(f.reveal.classList.contains('istok-motion-pending'), false);
  assert.equal(f.observers.length, 0);
  assert.equal(f.frames.size + f.intervals.size + f.timeouts.size, 0);
  assert.equal(f.rotator.children[0].textContent, 'совпадения');
  assert.equal(f.rotator.classList.contains('istok-motion-rotator'), false);
  assert.equal(f.rotator.getAttribute('aria-live'), null);
  f.window.IstokMotion.teardown();
});

test('rotations reserve every phrase and teardown clears all resources and stale callbacks', () => {
  const f = fixture();
  f.window.IstokMotion.mount(f.root);
  f.observers[0].trigger(f.count);
  const staleFrame = [...f.frames.values()][0];
  assert.equal(f.rotator.children.length, 3);
  assert.equal(f.intervals.size, 1);
  f.interval();
  assert.ok(f.rotator.classList.contains('istok-motion-rotate-out'));
  f.timeout();
  assert.ok(f.rotator.children[1].classList.contains('istok-motion-current'));
  f.interval();
  assert.equal(f.timeouts.size, 1);
  f.window.IstokMotion.teardown();
  assert.equal(f.frames.size + f.intervals.size + f.timeouts.size, 0);
  assert.ok(f.observers[0].disconnected);
  assert.equal(f.mediaListeners.size, 0);
  assert.equal(f.listeners.get('visibilitychange').size, 0);
  assert.equal(f.reveal.classList.contains('istok-motion-pending'), false);
  assert.equal(f.count.textContent, '2');
  staleFrame(5000);
  f.observers[0].trigger(f.count);
  assert.equal(f.count.textContent, '2');
  assert.equal(f.frames.size, 0);
});

test('background tabs pause frames and rotation, then resume without hidden-time catch-up', () => {
  const f = fixture();
  f.window.IstokMotion.mount(f.root);
  f.observers[0].trigger(f.count);
  f.frame(0);
  f.frame(100);
  const before = f.count.textContent;
  f.interval();
  f.visibility(true);
  assert.equal(f.frames.size + f.intervals.size + f.timeouts.size, 0);
  assert.equal(f.rotator.classList.contains('istok-motion-rotate-out'), false);
  f.frame(10000);
  assert.equal(f.count.textContent, before);
  f.visibility(false);
  assert.equal(f.intervals.size, 1);
  f.frame(11000);
  assert.equal(f.count.textContent, before);
  f.frame(11760);
  assert.equal(f.count.textContent, '2');
  f.window.IstokMotion.teardown();
});

test('intersection while hidden waits for visibility and reduced-motion changes finish statically', () => {
  const f = fixture();
  f.visibility(true);
  f.window.IstokMotion.mount(f.root);
  f.observers[0].trigger(f.reveal, f.count);
  assert.ok(f.reveal.classList.contains('istok-motion-pending'));
  assert.equal(f.frames.size + f.intervals.size, 0);
  f.visibility(false);
  assert.equal(f.reveal.classList.contains('istok-motion-pending'), false);
  assert.equal(f.count.textContent, '0');
  f.reduce(true);
  assert.equal(f.count.textContent, '2');
  assert.equal(f.frames.size + f.intervals.size + f.timeouts.size, 0);
  assert.ok(f.observers[0].disconnected);
  f.window.IstokMotion.teardown();
});

test('unlabelled, screen-reader-visible and live-region elements remain static', () => {
  const f = fixture({ rotate: false });
  f.count.setAttribute('aria-hidden', 'false');
  const unlabelled = f.root.append(new Element(f.document, { 'data-count': '4', 'aria-hidden': 'true' }, '4'));
  const live = f.root.append(new Element(f.document, { 'aria-label': 'Форматы поиска', 'aria-live': 'polite' }));
  const rotator = live.append(new Element(f.document, { 'data-rotate': '["один","два"]', 'aria-hidden': 'true' }, 'один'));
  f.window.IstokMotion.mount(f.root);
  assert.equal(f.observers[0].observed.has(f.count), false);
  assert.equal(f.observers[0].observed.has(unlabelled), false);
  assert.equal(rotator.textContent, 'один');
  assert.equal(f.intervals.size, 0);
  assert.equal(f.count.textContent, '2');
  f.window.IstokMotion.teardown();
});

test('mounting again tears down previous work and stale returned cleanup cannot stop the new root', () => {
  const f = fixture();
  const oldCleanup = f.window.IstokMotion.mount(f.root);
  const oldObserver = f.observers[0];
  f.window.IstokMotion.mount(f.root);
  assert.ok(oldObserver.disconnected);
  assert.equal(f.intervals.size, 1);
  assert.equal(f.listeners.get('visibilitychange').size, 1);
  oldCleanup();
  assert.equal(f.intervals.size, 1);
  assert.equal(f.observers[1].disconnected, false);
  f.window.IstokMotion.teardown();
});

test('observer construction failure never leaves pending invisible content', () => {
  const f = fixture({ rotate: false });
  f.window.IntersectionObserver = class { constructor() { throw new Error('unsupported'); } };
  f.window.IstokMotion.mount(f.root);
  assert.equal(f.reveal.classList.contains('istok-motion-pending'), false);
  assert.equal(f.count.textContent, '2');
  assert.equal(f.frames.size, 0);
  f.window.IstokMotion.teardown();
});
