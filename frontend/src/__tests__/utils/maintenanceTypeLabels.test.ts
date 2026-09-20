import { describe, it, expect } from 'vitest';
import { maintenanceTypeLabel } from '../../utils/maintenanceTypeLabels';

const t = (key: string, options?: Record<string, unknown>) => {
  const translations: Record<string, string> = {
    'maintenance.types.visionEncoderCalibration': 'Vision-Encoder-Kalibrierung',
    'maintenance.types.printerCalibration': 'Druckerkalibrierung',
    'maintenance.types.cleanBuildPlate': 'Druckplatte reinigen',
  };
  return translations[key] ?? (options?.defaultValue as string) ?? key;
};

describe('maintenanceTypeLabel', () => {
  it('translates the names the backend seeds its system types with', () => {
    expect(maintenanceTypeLabel('Vision Encoder Calibration', t)).toBe('Vision-Encoder-Kalibrierung');
    expect(maintenanceTypeLabel('Printer Calibration', t)).toBe('Druckerkalibrierung');
    expect(maintenanceTypeLabel('Clean Build Plate', t)).toBe('Druckplatte reinigen');
  });

  it('shows a custom or renamed type as stored', () => {
    expect(maintenanceTypeLabel('Grease the Z screws', t)).toBe('Grease the Z screws');
    expect(maintenanceTypeLabel('Clean build plate', t)).toBe('Clean build plate');
  });

  it('falls back to the stored name when a locale lacks the key', () => {
    expect(maintenanceTypeLabel('Check PTFE Tube', t)).toBe('Check PTFE Tube');
  });
});
