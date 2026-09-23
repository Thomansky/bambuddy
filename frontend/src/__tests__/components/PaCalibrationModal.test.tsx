/**
 * Tests for PaCalibrationModal — the confirmation dialog in front of a real
 * print.
 *
 * The rule this file exists to hold: Start is never a silently dead button.
 * Every reason the backend can refuse a run has to arrive in front of the
 * user, and the plate tick has to be given deliberately every time.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, cleanup, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { PaCalibrationModal } from '../../components/PaCalibrationModal';
import type { PaCalibrationPreflight } from '../../api/client';
import { api } from '../../api/client';

vi.mock('../../api/client', () => ({
  api: {
    getPaCalibrationPreflight: vi.fn(),
    createPaCalibrationRun: vi.fn(),
    getSettings: vi.fn().mockResolvedValue({}),
    getAuthStatus: vi.fn().mockResolvedValue({ auth_enabled: false }),
  },
}));

const READY: PaCalibrationPreflight = {
  supported: true,
  blocked_reasons: [],
  printer_model: 'H2S',
  nozzle_diameter: '0.4',
  nozzle_id: 'HS01-0.4',
  extruder_id: 0,
  filament: {
    filament_id: 'GFA01',
    setting_id: '',
    name: 'PLA Matte',
    material: 'PLA',
    colour: 'FFFFFFFF',
  },
  current_k: 0.02,
  current_cali_idx: 1,
  current_profile_name: 'Bambu PLA Matte',
  presets: {
    printer: { source: 'standard', id: 'Bambu Lab H2S 0.4 nozzle' },
    process: { source: 'standard', id: '0.20mm Standard @BBL H2S' },
    filament: { source: 'standard', id: 'Bambu PLA Matte @BBL H2S' },
  },
  plate_types: ['cool_plate', 'eng_plate', 'hot_plate', 'textured_plate'],
  default_plate_type: 'textured_plate',
  estimated_seconds: 420,
  estimated_grams: 0.1,
};

const props = {
  isOpen: true,
  printerId: 1,
  printerName: 'H2S',
  amsId: 0,
  slotId: 1,
  slotLabel: 'AMS 1 · 2',
  onClose: vi.fn(),
};

function preflight(overrides: Partial<PaCalibrationPreflight> = {}) {
  vi.mocked(api.getPaCalibrationPreflight).mockResolvedValue({ ...READY, ...overrides });
}

describe('PaCalibrationModal', () => {
  beforeEach(() => {
    vi.mocked(api.createPaCalibrationRun).mockResolvedValue({ id: 7 } as never);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('shows the K value the printer stores today', async () => {
    preflight();
    render(<PaCalibrationModal {...props} />);
    expect(await screen.findByText(/0\.020/)).toBeInTheDocument();
  });

  it('says how long it takes and what it costs', async () => {
    preflight();
    render(<PaCalibrationModal {...props} />);
    expect(await screen.findByText(/about 7 min/)).toBeInTheDocument();
  });

  // Each of these arrives from the backend's preflight; the UI never derives
  // its own. A disabled Start with no sentence next to it is the one outcome
  // this modal must never produce.
  it.each([
    ['printer_offline', 'The printer is offline.'],
    ['printer_busy', 'The printer is printing.'],
    ['slicer_not_configured', /slicer sidecar is not reachable/],
    ['slot_empty', 'The slot holds no identified filament.'],
    ['run_already_active', 'A calibration is already running on this printer.'],
  ])('disables Start and says why for %s', async (reason, text) => {
    preflight({ blocked_reasons: [reason] });
    render(<PaCalibrationModal {...props} />);

    expect(await screen.findByTestId(`pa-blocked-${reason}`)).toBeInTheDocument();
    expect(screen.getByText(text as string | RegExp)).toBeInTheDocument();
    expect(screen.getByTestId('pa-calibration-start')).toBeDisabled();
  });

  it('on an unsupported model says only that, and offers no form', async () => {
    preflight({
      supported: false,
      blocked_reasons: ['model_not_supported'],
      filament: null,
      presets: null,
      plate_types: [],
    });
    render(<PaCalibrationModal {...props} />);

    expect(await screen.findByText('Not supported on this printer yet.')).toBeInTheDocument();
    expect(screen.queryByTestId('pa-plate-confirm')).not.toBeInTheDocument();
    expect(screen.getByTestId('pa-calibration-start')).toBeDisabled();
  });

  it('will not start until the plate is confirmed empty', async () => {
    preflight();
    const user = userEvent.setup();
    render(<PaCalibrationModal {...props} />);

    // Not pre-ticked: the job prints on the bed, so this is an assertion about
    // the bed right now rather than a preference.
    const tick = await screen.findByTestId('pa-plate-confirm');
    const start = screen.getByTestId('pa-calibration-start');
    expect(tick).not.toBeChecked();
    expect(start).toBeDisabled();

    await user.click(tick);
    await waitFor(() => expect(start).toBeEnabled());
  });

  it('sends the slot, the plate and the tick', async () => {
    preflight();
    const user = userEvent.setup();
    render(<PaCalibrationModal {...props} />);

    await user.click(await screen.findByTestId('pa-plate-confirm'));
    await user.click(screen.getByTestId('pa-calibration-start'));

    await waitFor(() =>
      expect(api.createPaCalibrationRun).toHaveBeenCalledWith(1, {
        ams_id: 0,
        slot_id: 1,
        plate_type: 'textured_plate',
        presets: READY.presets,
        plate_confirmed: true,
      }),
    );
  });
});
