import { describe, it, expect } from 'vitest';
import { vatSuffix } from '../../utils/vat';

const t = (key: string) => key;

describe('vatSuffix', () => {
  it('is empty while the VAT distinction is off, whatever the basis says', () => {
    expect(vatSuffix({ vat_enabled: false, price_vat_basis: 'gross' }, t)).toBe('');
    expect(vatSuffix({ vat_enabled: false, price_vat_basis: 'net' }, t)).toBe('');
  });

  it('is empty before the settings have loaded', () => {
    expect(vatSuffix(undefined, t)).toBe('');
    expect(vatSuffix(null, t)).toBe('');
    expect(vatSuffix({}, t)).toBe('');
  });

  it('names the working basis once enabled', () => {
    expect(vatSuffix({ vat_enabled: true, price_vat_basis: 'gross' }, t)).toBe('common.vatIncl');
    expect(vatSuffix({ vat_enabled: true, price_vat_basis: 'net' }, t)).toBe('common.vatExcl');
  });

  it('treats a missing basis as gross, the settings default', () => {
    expect(vatSuffix({ vat_enabled: true }, t)).toBe('common.vatIncl');
  });
});
