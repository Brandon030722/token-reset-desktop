import { spawnSync } from 'node:child_process';
const result = spawnSync(process.platform === 'win32' ? 'python' : 'python3',
  ['-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py'], { stdio: 'inherit' });
process.exit(result.status ?? 1);
