/* Optional homepage motion. Static content remains usable without this module. */
(function (window, document) {
  'use strict';

  let dispose = null;

  function teardown() {
    if (dispose) dispose();
    dispose = null;
  }

  function mount(root) {
    teardown();
    if (!root || typeof root.querySelectorAll !== 'function') return teardown;

    const owner = root.ownerDocument || document;
    const media = typeof window.matchMedia === 'function'
      ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
    let active = true;
    let motion = !media?.matches;
    let observer = null;
    let frame = null;
    let previousTime = null;
    let interval = null;
    let swapTimer = null;
    const waiting = new Set();
    const runningCounts = new Set();
    const counts = new Map();
    const rotators = [];
    const find = selector => {
      const found = Array.from(root.querySelectorAll(selector));
      if (root.matches?.(selector)) found.unshift(root);
      return found;
    };
    const reveals = find('[data-reveal]');
    const hidden = () => owner.hidden || owner.visibilityState === 'hidden';

    function labelFor(element) {
      let found = '';
      for (let parent = element; parent; parent = parent.parentElement) {
        const live = parent.getAttribute('aria-live');
        if (live && live !== 'off') return '';
        const label = parent.getAttribute('aria-label');
        if (parent !== element && label?.trim() && !found) found = label.trim();
        if (parent === root) break;
      }
      return found;
    }

    for (const element of find('[data-count]')) {
      const value = element.getAttribute('data-count');
      const target = Number(value);
      if (!/^\d{1,2}$/.test(value || '') || target > 20 || element.getAttribute('aria-hidden') !== 'true') continue;
      const label = labelFor(element);
      if (!new RegExp('(^|\\D)' + target + '(\\D|$)').test(label)) continue;
      counts.set(element, { target, elapsed: 0, started: false });
      element.style.setProperty('--istok-count-width', String(target).length + 'ch');
    }

    for (const element of find('[data-rotate]')) {
      if (element.getAttribute('aria-hidden') !== 'true' || !labelFor(element)) continue;
      let phrases = Array.from(element.querySelectorAll('[data-motion-phrase]'));
      if (!phrases.length) {
        let values;
        try { values = JSON.parse(element.getAttribute('data-rotate')); } catch (_) { continue; }
        if (!Array.isArray(values) || values.length < 2 || values.length > 6
            || values.some(value => typeof value !== 'string' || !value.trim() || value.length > 90)) continue;
        phrases = values.map(value => {
          const phrase = owner.createElement('span');
          phrase.setAttribute('data-motion-phrase', '');
          phrase.textContent = value;
          return phrase;
        });
        element.replaceChildren(...phrases);
      }
      if (phrases.length < 2 || phrases.length > 6) continue;
      rotators.push({ element, phrases, index: 0 });
    }

    function stopFrame() {
      if (frame !== null) window.cancelAnimationFrame(frame);
      frame = null;
      previousTime = null;
    }

    function requestFrame() {
      if (active && motion && !hidden() && runningCounts.size && frame === null) {
        frame = window.requestAnimationFrame(tick);
      }
    }

    function tick(timestamp) {
      frame = null;
      if (!active || !motion || hidden()) { previousTime = null; return; }
      const delta = previousTime === null ? 0 : Math.max(0, timestamp - previousTime);
      previousTime = timestamp;
      for (const element of runningCounts) {
        const state = counts.get(element);
        state.elapsed = Math.min(760, state.elapsed + delta);
        const fraction = 1 - Math.pow(1 - state.elapsed / 760, 3);
        element.textContent = String(Math.round(state.target * fraction));
        if (state.elapsed === 760) runningCounts.delete(element);
      }
      if (!runningCounts.size) previousTime = null;
      requestFrame();
    }

    function show(element) {
      element.classList.remove('istok-motion-pending');
      waiting.delete(element);
      if (observer) observer.unobserve(element);
      const state = counts.get(element);
      if (state && !state.started) {
        state.started = true;
        if (motion && typeof window.requestAnimationFrame === 'function' && !hidden()) {
          element.textContent = '0';
          runningCounts.add(element);
          requestFrame();
        } else element.textContent = String(state.target);
      }
    }

    function stopRotation() {
      if (interval !== null) window.clearInterval(interval);
      if (swapTimer !== null) window.clearTimeout(swapTimer);
      interval = swapTimer = null;
      for (const state of rotators) state.element.classList.remove('istok-motion-rotate-out');
    }

    function startRotation() {
      if (!active || !motion || hidden() || !rotators.length || interval !== null) return;
      for (const state of rotators) {
        state.element.classList.add('istok-motion-rotator');
        state.phrases[state.index].classList.add('istok-motion-current');
      }
      interval = window.setInterval(() => {
        if (!active || hidden() || !motion) return;
        for (const state of rotators) state.element.classList.add('istok-motion-rotate-out');
        swapTimer = window.setTimeout(() => {
          swapTimer = null;
          if (!active || hidden() || !motion) return;
          for (const state of rotators) {
            state.phrases[state.index].classList.remove('istok-motion-current');
            state.index = (state.index + 1) % state.phrases.length;
            state.phrases[state.index].classList.add('istok-motion-current');
            state.element.classList.remove('istok-motion-rotate-out');
          }
        }, 160);
      }, 3200);
    }

    function staticContent() {
      stopFrame();
      stopRotation();
      if (observer) observer.disconnect();
      observer = null;
      waiting.clear();
      runningCounts.clear();
      for (const element of reveals) element.classList.remove('istok-motion-pending');
      for (const [element, state] of counts) element.textContent = String(state.target);
      for (const state of rotators) {
        state.index = 0;
        state.element.classList.remove('istok-motion-rotator');
        for (const phrase of state.phrases) phrase.classList.remove('istok-motion-current');
      }
    }

    function visibilityChanged() {
      if (!active) return;
      if (hidden()) { stopFrame(); stopRotation(); }
      else {
        for (const element of Array.from(waiting)) show(element);
        requestFrame();
        startRotation();
      }
    }

    function preferenceChanged() {
      if (media?.matches) { motion = false; staticContent(); }
    }

    if (motion && typeof window.IntersectionObserver === 'function') {
      try {
        observer = new window.IntersectionObserver(entries => {
          if (!active) return;
          for (const entry of entries) {
            if (!entry.isIntersecting) { waiting.delete(entry.target); continue; }
            if (hidden()) waiting.add(entry.target);
            else show(entry.target);
          }
        }, { threshold: 0.12, rootMargin: '0px 0px -18px 0px' });
        for (const element of reveals) element.classList.add('istok-motion-reveal', 'istok-motion-pending');
        for (const element of new Set([...reveals, ...counts.keys()])) observer.observe(element);
      } catch (_) {
        if (observer) observer.disconnect();
        observer = null;
        for (const element of reveals) element.classList.remove('istok-motion-pending');
        for (const [element, state] of counts) element.textContent = String(state.target);
      }
    } else {
      for (const [element, state] of counts) element.textContent = String(state.target);
    }
    if (motion) startRotation();
    owner.addEventListener('visibilitychange', visibilityChanged);
    if (media?.addEventListener) media.addEventListener('change', preferenceChanged);
    else if (media?.addListener) media.addListener(preferenceChanged);

    const cleanup = () => {
      if (!active) return;
      active = false;
      staticContent();
      owner.removeEventListener('visibilitychange', visibilityChanged);
      if (media?.removeEventListener) media.removeEventListener('change', preferenceChanged);
      else if (media?.removeListener) media.removeListener(preferenceChanged);
      for (const element of reveals) element.classList.remove('istok-motion-reveal');
      for (const element of counts.keys()) element.style.removeProperty('--istok-count-width');
    };
    dispose = cleanup;
    return cleanup;
  }

  window.IstokMotion = Object.freeze({ mount, teardown });
})(window, document);
