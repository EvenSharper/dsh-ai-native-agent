import { access, cp, mkdir } from 'node:fs/promises';

// `runtime/agent` is the shipped copy of the Python core, so it is committed
// here and always present. When this plugin is developed inside the agent
// repository (the core at ../../agent), refresh it from that single source of
// truth; when this repository stands alone, keep the committed copy as-is.
const candidates = [
  new URL('../../agent/', import.meta.url),   // ../agent, when plugin and core are siblings
  new URL('../../agent/agent/', import.meta.url), // <monorepo>/agent, when the plugin is nested
];
const target = new URL('../runtime/agent/', import.meta.url);

let source;
for (const candidate of candidates) {
  try {
    await access(new URL('harness_bridge.py', candidate));
    source = candidate;
    break;
  } catch {
    // Try the next layout.
  }
}

await mkdir(target, { recursive: true });

if (!source) {
  await access(new URL('harness_bridge.py', target));
  console.log('runtime/agent: no agent core next to this repository; using the committed copy.');
} else {
  await cp(source, target, {
    recursive: true,
    filter: (path) => !path.includes('__pycache__') && !path.endsWith('.pyc'),
  });
  console.log('runtime/agent: refreshed from ' + source.pathname);
}
