/**
 * How a picker lists projects: in tree order, each sub-project after its
 * parent, named with its running number and what it belongs to.
 *
 * Reported on a project "RAFI Group" whose sub-projects "RAFI" (4019) and
 * "Armpolster" (4020) showed up in the archive's project picker as bare names,
 * sorted in among everything else, so neither the number nor the fact that
 * "RAFI" is part of "RAFI Group" could be seen.
 */

import { describe, expect, it } from 'vitest';
import { projectChoices, projectText } from '../../utils/projectTree';

type Row = { id: number; name: string; number: string | null; parent_id: number | null };
const row = (id: number, name: string, number: string | null = null, parent_id: number | null = null): Row => ({
  id,
  name,
  number,
  parent_id,
});

const rafi = [
  row(1, 'Zubehör'),
  row(5, 'RAFI', '4019', 6),
  row(6, 'RAFI Group'),
  row(7, 'Armpolster', '4020', 6),
  row(8, 'Halterung', '4011'),
];

describe('projectText', () => {
  it('puts the running number before the name', () => {
    expect(projectText({ name: 'RAFI', number: '4019' })).toBe('4019 RAFI');
  });

  it('is the bare name without a number', () => {
    expect(projectText({ name: 'RAFI Group', number: null })).toBe('RAFI Group');
    expect(projectText({ name: 'RAFI Group' })).toBe('RAFI Group');
  });
});

describe('projectChoices', () => {
  it('lists each sub-project right after its parent, named with number and parent', () => {
    const choices = projectChoices(rafi);

    expect(choices.map((c) => c.path)).toEqual([
      '4011 Halterung',
      'RAFI Group',
      'RAFI Group › 4019 RAFI',
      'RAFI Group › 4020 Armpolster',
      'Zubehör',
    ]);
    expect(choices.map((c) => c.depth)).toEqual([0, 0, 1, 1, 0]);
  });

  it('carries the text and the parent apart for places that draw them separately', () => {
    const sub = projectChoices(rafi).find((c) => c.project.id === 5)!;

    expect(sub.text).toBe('4019 RAFI');
    expect(sub.parentPath).toBe('RAFI Group');
    expect(projectChoices(rafi).find((c) => c.project.id === 6)!.parentPath).toBeNull();
  });

  it('sorts numbers by value, not character by character', () => {
    const choices = projectChoices([row(1, 'B', '10'), row(2, 'A', '9'), row(3, 'C', '100')]);

    expect(choices.map((c) => c.text)).toEqual(['9 A', '10 B', '100 C']);
  });

  it('names every level of a deeper tree', () => {
    const choices = projectChoices([row(1, 'Farm'), row(2, 'Customer', null, 1), row(3, 'Order', '4021', 2)]);

    expect(choices.map((c) => c.path)).toEqual(['Farm', 'Farm › Customer', 'Farm › Customer › 4021 Order']);
    expect(choices.map((c) => c.depth)).toEqual([0, 1, 2]);
  });

  it('still names the parent when the picker does not offer it', () => {
    // An archived parent is left out of an archive picker; its active
    // sub-project stays on offer and must still say where it belongs.
    const offered = rafi.filter((p) => p.id !== 6);

    const choices = projectChoices(offered, rafi);

    const sub = choices.find((c) => c.project.id === 5)!;
    expect(sub.path).toBe('RAFI Group › 4019 RAFI');
    expect(sub.depth).toBe(0);
    expect(choices).toHaveLength(offered.length);
  });

  it('keeps projects caught in a parent cycle instead of dropping them', () => {
    const choices = projectChoices([row(1, 'Loop A', null, 2), row(2, 'Loop B', null, 1), row(3, 'Plain')]);

    expect(choices.map((c) => c.project.id).sort()).toEqual([1, 2, 3]);
  });

  it('offers nothing for nothing', () => {
    expect(projectChoices([])).toEqual([]);
  });
});
