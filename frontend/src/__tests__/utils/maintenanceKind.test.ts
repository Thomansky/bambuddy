/**
 * Manual versus automated classification (#3127). One helper decides what
 * the badge says on a card and on a type, what the per-printer counts add
 * up, and what the filter keeps — so they cannot drift apart.
 */

import { describe, it, expect } from 'vitest';
import {
  isAutomatedMaintenance,
  maintenanceKind,
  matchesKindFilter,
} from '../../utils/maintenanceKind';

describe('maintenanceKind', () => {
  it('calls an action item on an automatic trigger automatic', () => {
    expect(maintenanceKind('calibration', 'schedule')).toBe('automatic');
    expect(maintenanceKind('motion_precision', 'when_due')).toBe('automatic');
  });

  it('calls an action item on the manual trigger "runs on request"', () => {
    expect(maintenanceKind('calibration', 'manual')).toBe('onRequest');
  });

  it('calls a reminder item manual, whatever trigger it claims', () => {
    expect(maintenanceKind(null, 'manual')).toBe('manual');
    expect(maintenanceKind(null, 'schedule')).toBe('manual');
  });

  it('treats a type, which has no trigger, as automatable or not', () => {
    expect(maintenanceKind('calibration')).toBe('automatic');
    expect(maintenanceKind(null)).toBe('manual');
  });
});

describe('the counts and the filter split on "can Bambuddy do it"', () => {
  it('counts an on-request item as automated', () => {
    expect(isAutomatedMaintenance('calibration')).toBe(true);
    expect(isAutomatedMaintenance(null)).toBe(false);
  });

  it('keeps everything under "all"', () => {
    expect(matchesKindFilter('calibration', 'all')).toBe(true);
    expect(matchesKindFilter(null, 'all')).toBe(true);
  });

  it('keeps only the matching kind otherwise', () => {
    expect(matchesKindFilter('calibration', 'automatic')).toBe(true);
    expect(matchesKindFilter('calibration', 'manual')).toBe(false);
    expect(matchesKindFilter(null, 'manual')).toBe(true);
    expect(matchesKindFilter(null, 'automatic')).toBe(false);
  });
});
