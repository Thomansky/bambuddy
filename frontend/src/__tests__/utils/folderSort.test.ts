/**
 * Sorting folders by number: the folder's own number, else the digits its
 * name starts with, compared as numbers.
 */

import { describe, it, expect } from 'vitest';
import { compareFolderNumbers, folderSortNumber } from '../../utils/folderSort';

describe('folderSortNumber', () => {
  it('takes the folder number first', () => {
    expect(folderSortNumber({ number: '4024', name: '7200090094' })).toBe('4024');
    expect(folderSortNumber({ number: ' 001 ', name: 'EBZ' })).toBe('001');
  });

  it('falls back to the digits the name starts with', () => {
    expect(folderSortNumber({ number: null, name: '4016' })).toBe('4016');
    expect(folderSortNumber({ number: '', name: '003 Stübbe' })).toBe('003');
    expect(folderSortNumber({ name: 'B.70925843 - 10' })).toBeNull();
    expect(folderSortNumber({ number: null, name: 'EBZ' })).toBeNull();
    expect(folderSortNumber({ name: '3D-Teile' })).toBeNull();
    expect(folderSortNumber({ name: '3MF Vorlagen' })).toBeNull();
    expect(folderSortNumber({ name: '4016-Halter' })).toBe('4016');
  });
});

describe('compareFolderNumbers', () => {
  it('compares digits as numbers, not as text', () => {
    const sorted = ['099', '4024', '05', '001', '40100', '4016'].sort(compareFolderNumbers);
    expect(sorted).toEqual(['001', '05', '099', '4016', '4024', '40100']);
  });

  it('still orders numbers that carry letters', () => {
    expect(['A-10', 'A-9', 'B-1'].sort(compareFolderNumbers)).toEqual(['A-9', 'A-10', 'B-1']);
  });
});
