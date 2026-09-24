/**
 * How many path-bar crumbs fit — the arithmetic, without a DOM.
 *
 * The rule the component relies on: a measurable bar answers with a number,
 * an unmeasurable one answers with null so the caller can keep its depth
 * fallback instead of folding everything or nothing.
 */

import { describe, it, expect } from 'vitest';
import { fitPathCrumbs, PATH_BAR_MIN_CRUMBS } from '../../utils/pathBarFit';

const base = {
  available: 1000,
  gap: 2,
  rootWidth: 100,
  ellipsisWidth: 30,
  crumbWidths: [] as number[],
  leafWidth: 0,
  reservedWidth: 0,
};

describe('fitPathCrumbs', () => {
  it('shows every crumb when the chain fits, however deep it is', () => {
    expect(fitPathCrumbs({ ...base, crumbWidths: [40, 40, 40, 40, 40] })).toBe(5);
  });

  it('folds only as much as it has to', () => {
    // 100 root + 5 * (120 + 2 gap) = 710; 400 of width leaves room for the
    // ellipsis (30 + 2) and two crumbs (244), not three.
    expect(fitPathCrumbs({ ...base, available: 400, crumbWidths: [120, 120, 120, 120, 120] })).toBe(2);
    expect(fitPathCrumbs({ ...base, available: 550, crumbWidths: [120, 120, 120, 120, 120] })).toBe(3);
  });

  it('does not collapse four short names that fit', () => {
    expect(fitPathCrumbs({ ...base, available: 400, crumbWidths: [50, 50, 50, 50] })).toBe(4);
  });

  it('keeps the last two crumbs even when nothing fits', () => {
    expect(fitPathCrumbs({ ...base, available: 10, crumbWidths: [500, 500, 500, 500] })).toBe(
      PATH_BAR_MIN_CRUMBS,
    );
  });

  it('never claims more crumbs than the chain has', () => {
    expect(fitPathCrumbs({ ...base, available: 10, crumbWidths: [500] })).toBe(1);
    expect(fitPathCrumbs({ ...base, available: 10, crumbWidths: [] })).toBe(0);
  });

  it('counts the leaf and the trailing controls against the available width', () => {
    const crumbWidths = [120, 120, 120];
    expect(fitPathCrumbs({ ...base, available: 500, crumbWidths })).toBe(3);
    expect(fitPathCrumbs({ ...base, available: 500, crumbWidths, leafWidth: 150 })).toBe(2);
    expect(fitPathCrumbs({ ...base, available: 500, crumbWidths, reservedWidth: 150 })).toBe(2);
  });

  it('answers null when nothing can be measured, so the caller can fall back', () => {
    expect(fitPathCrumbs({ ...base, available: 0, crumbWidths: [40, 40] })).toBeNull();
    expect(fitPathCrumbs({ ...base, rootWidth: 0, crumbWidths: [40, 40] })).toBeNull();
    expect(fitPathCrumbs({ ...base, crumbWidths: [0, 0, 0, 0] })).toBeNull();
  });
});
