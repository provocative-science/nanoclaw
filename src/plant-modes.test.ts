import fs from 'fs';
import path from 'path';

import { afterAll, beforeEach, describe, expect, it, vi } from 'vitest';

const tmp = vi.hoisted(() => {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { mkdtempSync } = require('fs') as typeof import('fs');
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { join } = require('path') as typeof import('path');
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { tmpdir } = require('os') as typeof import('os');
  return mkdtempSync(join(tmpdir(), 'nanoclaw-modes-'));
});

vi.mock('./config.js', () => ({ DATA_DIR: `${tmp}/data` }));
vi.mock('./logger.js', () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

import {
  PLANT_MODES_FILE,
  describePlantModes,
  handlePlantModeIpc,
  parsePlantModeCommand,
  readPlantModes,
  setPlantMode,
} from './plant-modes.js';

const NOW = Date.parse('2026-11-05T18:00:00Z');

beforeEach(() => fs.rmSync(PLANT_MODES_FILE, { force: true }));
afterAll(() => fs.rmSync(tmp, { recursive: true, force: true }));

describe('parsePlantModeCommand', () => {
  it('parses scoped mute commands with durations', () => {
    expect(
      parsePlantModeCommand('@Ghost mute container alerts for 12h'),
    ).toMatchObject({
      subsystem: 'container',
      mode: 'muted',
      until: '12h',
    });
    expect(
      parsePlantModeCommand('@Ghost unmute liquifaction alerts'),
    ).toMatchObject({
      subsystem: 'liquifaction',
      mode: 'in_service',
    });
  });

  it('parses whole-message declarations only', () => {
    expect(parsePlantModeCommand('@Ghost container offline')).toMatchObject({
      mode: 'offline',
    });
    expect(
      parsePlantModeCommand('@Ghost liquefaction maintenance for 3 hours'),
    ).toMatchObject({
      mode: 'maintenance',
      until: '3 hours',
    });
    expect(
      parsePlantModeCommand('@Ghost container back in service'),
    ).toMatchObject({
      mode: 'in_service',
    });
    expect(
      parsePlantModeCommand('@Ghost is the container offline?'),
    ).toBeNull();
    expect(
      parsePlantModeCommand('@Ghost why was the container offline yesterday'),
    ).toBeNull();
    expect(parsePlantModeCommand('@Ghost mute alerts')).toBeNull();
  });
});

describe('setPlantMode', () => {
  it('writes the monitor file and clears it on in_service', () => {
    const r = setPlantMode(
      {
        subsystem: 'container',
        mode: 'offline',
        reason: 'idle',
        setBy: 'Spark',
      },
      NOW,
    );
    expect(r.ok).toBe(true);
    expect(readPlantModes().subsystems.container).toMatchObject({
      mode: 'offline',
      until: null,
      set_by: 'Spark',
    });
    expect(describePlantModes(NOW)[0]).toContain(
      'container: OFFLINE since Nov 5, 13:00 ET (2026-11-05T18:00Z) by Spark',
    );
    expect(
      setPlantMode(
        { subsystem: 'container', mode: 'in_service', setBy: 'Spark' },
        NOW,
      ).message,
    ).toContain('back in service');
    expect(readPlantModes().subsystems.container).toBeUndefined();
  });

  it('resolves durations, defaults maintenance to 4h, rejects bad until', () => {
    setPlantMode(
      { subsystem: 'liquefaction', mode: 'muted', until: '90m', setBy: 'x' },
      NOW,
    );
    expect(readPlantModes().subsystems.liquefaction?.until).toBe(
      '2026-11-05T19:30:00.000Z',
    );
    setPlantMode({ subsystem: 'airbed', mode: 'maintenance', setBy: 'x' }, NOW);
    expect(readPlantModes().subsystems.airbed?.until).toBe(
      '2026-11-05T22:00:00.000Z',
    );
    expect(
      setPlantMode(
        {
          subsystem: 'airbed',
          mode: 'muted',
          until: '2026-11-05T12:00',
          setBy: 'x',
        },
        NOW,
      ).ok,
    ).toBe(false);
    expect(
      setPlantMode(
        {
          subsystem: 'airbed',
          mode: 'muted',
          until: '2026-11-04T12:00Z',
          setBy: 'x',
        },
        NOW,
      ).ok,
    ).toBe(false);
    expect(
      setPlantMode({ subsystem: 'pirate', mode: 'muted', setBy: 'x' }, NOW).ok,
    ).toBe(false);
  });

  it('mutes and unmutes liquefaction bits', () => {
    const r = setPlantMode(
      {
        subsystem: 'liquefaction',
        mode: 'muted',
        bits: { severity: 'warning', bits: [2, 2, 5] },
        setBy: 'x',
      },
      NOW,
    );
    expect(r.ok).toBe(true);
    expect(readPlantModes().muted_bits.map((b) => b.bit)).toEqual([2, 5]);
    setPlantMode(
      {
        subsystem: 'liquefaction',
        mode: 'in_service',
        bits: { severity: 'warning', bits: [2] },
        setBy: 'x',
      },
      NOW,
    );
    expect(readPlantModes().muted_bits.map((b) => b.bit)).toEqual([5]);
    expect(
      setPlantMode(
        {
          subsystem: 'container',
          mode: 'muted',
          bits: { severity: 'warning', bits: [1] },
          setBy: 'x',
        },
        NOW,
      ).ok,
    ).toBe(false);
  });
});

describe('handlePlantModeIpc', () => {
  it('writes a result file for the calling group', () => {
    handlePlantModeIpc(
      {
        requestId: 'r1',
        subsystem: 'container',
        mode: 'offline',
        reason: 'idle',
        set_by: 'Spark',
      },
      'telegram_carbon-capture',
      'tg:-100',
    );
    const p = path.join(
      tmp,
      'data',
      'ipc',
      'telegram_carbon-capture',
      'input',
      'plant_mode_result_r1.json',
    );
    expect(JSON.parse(fs.readFileSync(p, 'utf-8'))).toMatchObject({
      type: 'plant_mode_result',
      ok: true,
    });
    expect(readPlantModes().subsystems.container).toMatchObject({
      set_by: 'Spark via telegram_carbon-capture',
      chat_jid: 'tg:-100',
    });
  });
});
