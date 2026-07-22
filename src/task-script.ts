/**
 * Host-side scheduled-task script runner (same wakeAgent JSON contract as
 * container/agent-runner runScript).
 */

import { execFile } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';

import { logger } from './logger.js';

export interface TaskScriptResult {
  wakeAgent: boolean;
  data?: unknown;
}

const SCRIPT_TIMEOUT_MS = 30_000;

export async function runTaskScript(
  script: string,
): Promise<TaskScriptResult | null> {
  const scriptPath = path.join(
    os.tmpdir(),
    `nanoclaw-task-script-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.sh`,
  );
  fs.writeFileSync(scriptPath, script, { mode: 0o755 });

  try {
    return await new Promise((resolve) => {
      execFile(
        'bash',
        [scriptPath],
        {
          timeout: SCRIPT_TIMEOUT_MS,
          maxBuffer: 1024 * 1024,
          env: process.env,
        },
        (error, stdout, stderr) => {
          if (stderr) {
            logger.debug(
              { stderr: stderr.slice(0, 500) },
              'Task script stderr',
            );
          }

          if (error) {
            logger.warn({ err: error.message }, 'Task script error');
            return resolve(null);
          }

          const lines = stdout.trim().split('\n');
          const lastLine = lines[lines.length - 1];
          if (!lastLine) {
            logger.warn('Task script produced no output');
            return resolve(null);
          }

          try {
            const result = JSON.parse(lastLine);
            if (typeof result.wakeAgent !== 'boolean') {
              logger.warn(
                { line: lastLine.slice(0, 200) },
                'Task script output missing wakeAgent boolean',
              );
              return resolve(null);
            }
            resolve(result as TaskScriptResult);
          } catch {
            logger.warn(
              { line: lastLine.slice(0, 200) },
              'Task script output is not valid JSON',
            );
            resolve(null);
          }
        },
      );
    });
  } finally {
    try {
      fs.unlinkSync(scriptPath);
    } catch {
      /* ignore */
    }
  }
}
