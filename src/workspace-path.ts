/**
 * Resolve container /workspace/... paths to host filesystem paths for outbound
 * Telegram media. Only paths that match real container mounts are allowed.
 */
import fs from 'fs';
import path from 'path';

import { GROUPS_DIR } from './config.js';
import { resolveGroupFolderPath } from './group-folder.js';
import { validateAdditionalMounts } from './mount-security.js';
import { RegisteredGroup } from './types.js';

/** Image extensions we accept for Telegram photo IPC (workspace files only). */
const IMAGE_EXT_RE = /\.(png|jpe?g|gif|webp|bmp|tiff?)$/i;

export function isWorkspaceImageFilename(filePath: string): boolean {
  const base = path.basename(filePath);
  return IMAGE_EXT_RE.test(base);
}

function isUnderDirectory(baseReal: string, candidateReal: string): boolean {
  const rel = path.relative(baseReal, candidateReal);
  return rel !== '' && !rel.startsWith('..') && !path.isAbsolute(rel);
}

function unsafePath(p: string): boolean {
  if (!p || p.includes('\0')) return true;
  const n = p.replace(/\\/g, '/');
  return (
    n.includes('/../') || n.startsWith('../') || n.endsWith('/..') || n === '..'
  );
}

/**
 * Map a path as seen inside the agent container to an absolute host path.
 * Returns null if the path is not allowed, escapes a mount, or does not exist.
 */
export function resolveContainerWorkspacePathToHost(
  containerPath: string,
  groupFolder: string,
  isMain: boolean,
  group: RegisteredGroup | undefined,
): string | null {
  const raw = containerPath.trim();
  if (unsafePath(raw)) return null;

  const normalized = path.posix.normalize(raw.replace(/\\/g, '/'));
  if (unsafePath(normalized)) return null;

  let groupDirReal: string;
  let projectRootReal: string;
  try {
    groupDirReal = fs.realpathSync(resolveGroupFolderPath(groupFolder));
    projectRootReal = fs.realpathSync(path.resolve(process.cwd()));
  } catch {
    return null;
  }

  let candidate: string | null = null;

  if (normalized.startsWith('/workspace/group/')) {
    const rel = normalized.slice('/workspace/group/'.length);
    if (!rel) return null;
    candidate = path.resolve(groupDirReal, rel);
  } else if (normalized.startsWith('/workspace/global/')) {
    const globalDir = path.join(GROUPS_DIR, 'global');
    if (!fs.existsSync(globalDir)) return null;
    let globalReal: string;
    try {
      globalReal = fs.realpathSync(globalDir);
    } catch {
      return null;
    }
    const rel = normalized.slice('/workspace/global/'.length);
    if (!rel) return null;
    candidate = path.resolve(globalReal, rel);
    if (!isUnderDirectory(globalReal, candidate)) return null;
  } else if (normalized.startsWith('/workspace/shared/')) {
    const sharedDir = path.join(GROUPS_DIR, 'global', 'shared');
    if (!fs.existsSync(sharedDir)) return null;
    let sharedReal: string;
    try {
      sharedReal = fs.realpathSync(sharedDir);
    } catch {
      return null;
    }
    const rel = normalized.slice('/workspace/shared/'.length);
    if (!rel) return null;
    candidate = path.resolve(sharedReal, rel);
    if (!isUnderDirectory(sharedReal, candidate)) return null;
  } else if (normalized.startsWith('/workspace/project/') && isMain) {
    const rel = normalized.slice('/workspace/project/'.length);
    if (!rel) return null;
    candidate = path.resolve(projectRootReal, rel);
    if (!isUnderDirectory(projectRootReal, candidate)) return null;
  } else if (normalized.startsWith('/workspace/extra/') && group) {
    const validated = validateAdditionalMounts(
      group.containerConfig?.additionalMounts ?? [],
      group.name,
      isMain,
    );
    const sorted = [...validated].sort(
      (a, b) => b.containerPath.length - a.containerPath.length,
    );
    const normMount = (cp: string) => cp.replace(/\\/g, '/');
    let matched: (typeof validated)[0] | null = null;
    let suffix = '';
    for (const m of sorted) {
      const cp = normMount(m.containerPath);
      if (normalized === cp) {
        matched = m;
        suffix = '';
        break;
      }
      const prefix = `${cp}/`;
      if (normalized.startsWith(prefix)) {
        matched = m;
        suffix = normalized.slice(prefix.length);
        break;
      }
    }
    if (!matched) return null;
    let mountHostReal: string;
    try {
      mountHostReal = fs.realpathSync(matched.hostPath);
    } catch {
      return null;
    }
    candidate = suffix ? path.resolve(mountHostReal, suffix) : mountHostReal;
    if (suffix && !isUnderDirectory(mountHostReal, candidate)) return null;
  } else {
    return null;
  }

  let real: string;
  try {
    real = fs.realpathSync(candidate);
  } catch {
    return null;
  }

  if (normalized.startsWith('/workspace/group/')) {
    if (!isUnderDirectory(groupDirReal, real)) return null;
  } else if (normalized.startsWith('/workspace/global/')) {
    const globalReal = fs.realpathSync(path.join(GROUPS_DIR, 'global'));
    if (!isUnderDirectory(globalReal, real)) return null;
  } else if (normalized.startsWith('/workspace/shared/')) {
    const sharedReal = fs.realpathSync(
      path.join(GROUPS_DIR, 'global', 'shared'),
    );
    if (!isUnderDirectory(sharedReal, real)) return null;
  } else if (normalized.startsWith('/workspace/project/')) {
    if (!isUnderDirectory(projectRootReal, real)) return null;
  } else if (normalized.startsWith('/workspace/extra/')) {
    const validated = validateAdditionalMounts(
      group?.containerConfig?.additionalMounts ?? [],
      group?.name ?? '',
      isMain,
    );
    const allowed = validated.some((m) => {
      let mh: string;
      try {
        mh = fs.realpathSync(m.hostPath);
      } catch {
        return false;
      }
      return real === mh || isUnderDirectory(mh, real);
    });
    if (!allowed) return null;
  }

  let st: fs.Stats;
  try {
    st = fs.statSync(real);
  } catch {
    return null;
  }
  if (!st.isFile()) return null;

  if (!isWorkspaceImageFilename(real)) return null;

  return real;
}
