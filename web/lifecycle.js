// Small lifecycle primitives kept independent of ComfyUI and the DOM for testing.
export function latestRequest(parentSignal) {
  let current;
  return {
    next() {
      current?.abort();
      const controller = new AbortController();
      current = controller;
      const abort = () => controller.abort();
      if (parentSignal?.aborted) abort();
      else parentSignal?.addEventListener('abort', abort, { once: true });
      return {
        signal: controller.signal,
        current: () => current === controller && !controller.signal.aborted,
        finish: () => parentSignal?.removeEventListener('abort', abort),
      };
    },
    dispose() { current?.abort(); },
  };
}

export function retainedResources() {
  const items = new Map();
  return {
    get(key, create) {
      if (!items.has(key)) items.set(key, create());
      return items.get(key);
    },
    prune(keys) {
      const keep = new Set(keys);
      for (const [key, item] of items) {
        if (!keep.has(key)) { item.dispose?.(); items.delete(key); }
      }
    },
    dispose() { this.prune([]); },
  };
}

export function releaseMedia(media) {
  media.pause?.();
  media.removeAttribute('src');
  media.load?.();
}
