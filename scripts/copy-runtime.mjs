import { cp, mkdir } from 'node:fs/promises';
const target = new URL('../runtime/agent/', import.meta.url);
await mkdir(target, { recursive: true });
await cp(new URL('../../agent/', import.meta.url), target, {
  recursive: true,
  filter: (path) => !path.includes('__pycache__') && !path.endsWith('.pyc'),
});
