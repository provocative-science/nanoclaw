import fs from 'fs';
import path from 'path';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';

import { GROUPS_DIR } from './config.js';
import {
  isSendableWorkspaceFilename,
  isWorkspaceImageFilename,
  resolveContainerWorkspacePathToHost,
} from './workspace-path.js';
import type { RegisteredGroup } from './types.js';

const TEST_FOLDER = 'z_wsptest';

describe('workspace-path', () => {
  beforeAll(() => {
    const dir = path.join(GROUPS_DIR, TEST_FOLDER);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'shot.png'), 'not-a-real-png');
  });

  afterAll(() => {
    fs.rmSync(path.join(GROUPS_DIR, TEST_FOLDER), {
      recursive: true,
      force: true,
    });
  });

  describe('isWorkspaceImageFilename', () => {
    it('accepts common raster extensions', () => {
      expect(isWorkspaceImageFilename('/a/b.PNG')).toBe(true);
      expect(isWorkspaceImageFilename('x.jpeg')).toBe(true);
      expect(isWorkspaceImageFilename('y.webp')).toBe(true);
    });

    it('rejects non-images', () => {
      expect(isWorkspaceImageFilename('x.txt')).toBe(false);
      expect(isWorkspaceImageFilename('doc.pdf')).toBe(false);
    });
  });

  describe('isSendableWorkspaceFilename', () => {
    it('accepts ordinary files of any extension', () => {
      expect(isSendableWorkspaceFilename('/workspace/group/report.md')).toBe(
        true,
      );
      expect(isSendableWorkspaceFilename('/workspace/group/a/b/data.csv')).toBe(
        true,
      );
      expect(isSendableWorkspaceFilename('noext')).toBe(true);
    });

    it('refuses dotfiles and dot-directories anywhere in the path', () => {
      expect(isSendableWorkspaceFilename('/workspace/group/.mcp.json')).toBe(
        false,
      );
      expect(isSendableWorkspaceFilename('/workspace/group/.env')).toBe(false);
      expect(
        isSendableWorkspaceFilename('/workspace/project/.git/config'),
      ).toBe(false);
      expect(isSendableWorkspaceFilename('')).toBe(false);
    });
  });

  describe('resolveContainerWorkspacePathToHost', () => {
    const group: RegisteredGroup = {
      name: 'wsptest',
      folder: TEST_FOLDER,
      trigger: '@x',
      added_at: new Date().toISOString(),
    };

    it('resolves /workspace/group/… to the host group directory', () => {
      const host = resolveContainerWorkspacePathToHost(
        '/workspace/group/shot.png',
        TEST_FOLDER,
        true,
        group,
      );
      expect(host).toBeTruthy();
      expect(fs.existsSync(host!)).toBe(true);
      expect(path.basename(host!)).toBe('shot.png');
    });

    it('returns null for paths outside allowed prefixes', () => {
      expect(
        resolveContainerWorkspacePathToHost(
          '/workspace/ipc/messages/x.json',
          TEST_FOLDER,
          true,
          group,
        ),
      ).toBeNull();
    });

    it('returns null for non-image files', () => {
      const note = path.join(GROUPS_DIR, TEST_FOLDER, 'notes.txt');
      fs.writeFileSync(note, 'hello');
      expect(
        resolveContainerWorkspacePathToHost(
          '/workspace/group/notes.txt',
          TEST_FOLDER,
          true,
          group,
        ),
      ).toBeNull();
    });

    it("resolves non-image files when kind is 'file'", () => {
      const md = path.join(GROUPS_DIR, TEST_FOLDER, 'report.md');
      fs.writeFileSync(md, '# hi');
      const host = resolveContainerWorkspacePathToHost(
        '/workspace/group/report.md',
        TEST_FOLDER,
        false,
        group,
        'file',
      );
      expect(host).toBeTruthy();
      expect(path.basename(host!)).toBe('report.md');
    });

    it("refuses dotfiles even when kind is 'file'", () => {
      const secret = path.join(GROUPS_DIR, TEST_FOLDER, '.mcp.json');
      fs.writeFileSync(secret, '{}');
      expect(
        resolveContainerWorkspacePathToHost(
          '/workspace/group/.mcp.json',
          TEST_FOLDER,
          true,
          group,
          'file',
        ),
      ).toBeNull();
    });

    it("refuses a symlink that resolves to a dotfile when kind is 'file'", () => {
      const secret = path.join(GROUPS_DIR, TEST_FOLDER, '.env');
      fs.writeFileSync(secret, 'X=1');
      const link = path.join(GROUPS_DIR, TEST_FOLDER, 'innocent.txt');
      fs.symlinkSync(secret, link);
      expect(
        resolveContainerWorkspacePathToHost(
          '/workspace/group/innocent.txt',
          TEST_FOLDER,
          true,
          group,
          'file',
        ),
      ).toBeNull();
    });

    it('still rejects traversal and unknown prefixes for files', () => {
      expect(
        resolveContainerWorkspacePathToHost(
          '/workspace/group/../../package.json',
          TEST_FOLDER,
          true,
          group,
          'file',
        ),
      ).toBeNull();
      expect(
        resolveContainerWorkspacePathToHost(
          '/workspace/ipc/messages/x.json',
          TEST_FOLDER,
          true,
          group,
          'file',
        ),
      ).toBeNull();
    });

    it('resolves /workspace/global/… for all groups including main', () => {
      const globalDir = path.join(GROUPS_DIR, 'global');
      fs.mkdirSync(globalDir, { recursive: true });
      const img = path.join(globalDir, 'wsptest-global.png');
      fs.writeFileSync(img, 'x');
      const host = resolveContainerWorkspacePathToHost(
        '/workspace/global/wsptest-global.png',
        TEST_FOLDER,
        true,
        group,
      );
      expect(host).toBeTruthy();
      expect(path.basename(host!)).toBe('wsptest-global.png');
      fs.unlinkSync(img);
    });

    it('resolves /workspace/shared/… for all groups', () => {
      const sharedDir = path.join(GROUPS_DIR, 'global', 'shared');
      fs.mkdirSync(sharedDir, { recursive: true });
      const img = path.join(sharedDir, 'wsptest-shared.png');
      fs.writeFileSync(img, 'x');
      const host = resolveContainerWorkspacePathToHost(
        '/workspace/shared/wsptest-shared.png',
        TEST_FOLDER,
        false,
        group,
      );
      expect(host).toBeTruthy();
      expect(path.basename(host!)).toBe('wsptest-shared.png');
      fs.unlinkSync(img);
    });
  });
});
