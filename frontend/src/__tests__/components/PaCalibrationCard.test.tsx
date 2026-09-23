/**
 * Tests for PaCalibrationCard — the live run, and the decision at the end of
 * it.
 *
 * Two contracts matter here. The card reads the run from the API rather than
 * holding it, because the run outlives the component: seven minutes of
 * printing and then a wait for a person. And Discard writes nothing — it must
 * not reach the route that publishes to the printer.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { screen, cleanup, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { PaCalibrationCard } from '../../components/PaCalibrationCard';
import type { PaCalibrationRun, PaCalibrationStatus } from '../../api/client';
import { api } from '../../api/client';

vi.mock('../../api/client', () => ({
  api: {
    getPaCalibrationRuns: vi.fn(),
    confirmPaCalibrationRun: vi.fn(),
    discardPaCalibrationRun: vi.fn(),
    cancelPaCalibrationRun: vi.fn(),
    getSettings: vi.fn().mockResolvedValue({}),
    getAuthStatus: vi.fn().mockResolvedValue({ auth_enabled: false }),
  },
}));

function run(overrides: Partial<PaCalibrationRun> = {}): PaCalibrationRun {
  return {
    id: 7,
    printer_id: 1,
    ams_id: 0,
    slot_id: 1,
    tray_id: 1,
    extruder_id: 0,
    filament_id: 'GFA01',
    filament_name: 'PLA Matte',
    nozzle_diameter: '0.4',
    nozzle_id: 'HS01-0.4',
    plate_type: 'textured_plate',
    plate_confirmed: true,
    method: 'sliced_print',
    status: 'printing',
    stage: 'printing',
    waiting_reason: null,
    waiting_detail: null,
    progress: 42,
    k_before: 0.02,
    k_value: 0.018612,
    n_coef: '0.750000',
    confidence: 0,
    error_message: null,
    created_at: '2026-09-20T10:31:00',
    started_at: '2026-09-20T10:31:34',
    completed_at: null,
    ...overrides,
  };
}

describe('PaCalibrationCard', () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it.each<[PaCalibrationStatus, string]>([
    ['queued', 'Waiting for the printer'],
    ['slicing', 'Slicing'],
    ['uploading', 'Uploading'],
    ['printing', 'Printing'],
    ['reading_result', 'Reading the result'],
    ['awaiting_confirmation', 'Waiting for your decision'],
    ['saving', 'Saving to the printer'],
    ['done', 'Saved'],
    ['failed', 'Failed'],
    ['cancelled', 'Cancelled'],
  ])('names the %s stage', (status, label) => {
    render(<PaCalibrationCard printerId={1} run={run({ status })} />);
    expect(screen.getByTestId('pa-calibration-stage')).toHaveTextContent(label);
  });

  it('says what it is waiting for while it is queued', () => {
    render(<PaCalibrationCard printerId={1} run={run({ status: 'queued', waiting_reason: 'printer_busy' })} />);
    expect(screen.getByTestId('pa-calibration-waiting')).toHaveTextContent(
      'Waiting for the printer to finish its current job',
    );
  });

  it('renders nothing when there is no live run', () => {
    render(<PaCalibrationCard printerId={1} run={null} />);
    expect(screen.queryByTestId('pa-calibration-card')).not.toBeInTheDocument();
  });

  it('survives a remount, because the run comes from the API', async () => {
    vi.mocked(api.getPaCalibrationRuns).mockResolvedValue({ runs: [run({ status: 'printing' })] });

    const first = render(<PaCalibrationCard printerId={1} />);
    expect(await screen.findByTestId('pa-calibration-card')).toHaveAttribute('data-status', 'printing');

    first.unmount();

    render(<PaCalibrationCard printerId={1} />);
    expect(await screen.findByTestId('pa-calibration-card')).toHaveAttribute('data-status', 'printing');
  });

  describe('the decision', () => {
    it('shows old next to new and says it will overwrite', () => {
      render(<PaCalibrationCard printerId={1} run={run({ status: 'awaiting_confirmation' })} />);

      const result = screen.getByTestId('pa-calibration-result');
      expect(result).toHaveTextContent('0.020');
      expect(screen.getByTestId('pa-calibration-new-k')).toHaveTextContent('0.019');
      expect(screen.getByTestId('pa-calibration-write-mode')).toHaveTextContent(
        'Saving replaces the existing K profile.',
      );
      expect(result).toHaveTextContent('0.750000');
    });

    it('says it will create one when the printer held no profile', () => {
      render(<PaCalibrationCard printerId={1} run={run({ status: 'awaiting_confirmation', k_before: null })} />);
      expect(screen.getByTestId('pa-calibration-write-mode')).toHaveTextContent(
        'Saving creates a new K profile and binds it to this slot.',
      );
    });

    it('writes only when Save is pressed', async () => {
      const user = userEvent.setup();
      vi.mocked(api.confirmPaCalibrationRun).mockResolvedValue(run({ status: 'done' }));
      render(<PaCalibrationCard printerId={1} run={run({ status: 'awaiting_confirmation' })} />);

      await user.click(screen.getByTestId('pa-calibration-confirm'));
      await waitFor(() => expect(api.confirmPaCalibrationRun).toHaveBeenCalledWith(1, 7));
    });

    it('discard makes no call that could reach the printer', async () => {
      const user = userEvent.setup();
      vi.mocked(api.discardPaCalibrationRun).mockResolvedValue(run({ status: 'cancelled' }));
      render(<PaCalibrationCard printerId={1} run={run({ status: 'awaiting_confirmation' })} />);

      await user.click(screen.getByTestId('pa-calibration-discard'));

      await waitFor(() => expect(api.discardPaCalibrationRun).toHaveBeenCalledWith(1, 7));
      expect(api.confirmPaCalibrationRun).not.toHaveBeenCalled();
    });

    it('offers no cancel once the decision is the only thing left', () => {
      render(<PaCalibrationCard printerId={1} run={run({ status: 'awaiting_confirmation' })} />);
      expect(screen.queryByTestId('pa-calibration-cancel')).not.toBeInTheDocument();
    });
  });

  describe('cancelling', () => {
    it('stops the print only after the user confirms it', async () => {
      const user = userEvent.setup();
      vi.mocked(api.cancelPaCalibrationRun).mockResolvedValue(run({ status: 'cancelled' }));
      const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
      render(<PaCalibrationCard printerId={1} run={run({ status: 'printing' })} />);

      await user.click(screen.getByTestId('pa-calibration-cancel'));
      expect(api.cancelPaCalibrationRun).not.toHaveBeenCalled();

      confirm.mockReturnValue(true);
      await user.click(screen.getByTestId('pa-calibration-cancel'));
      await waitFor(() => expect(api.cancelPaCalibrationRun).toHaveBeenCalledWith(1, 7, true));
      confirm.mockRestore();
    });

    it('needs no confirmation before the printer has the job', async () => {
      const user = userEvent.setup();
      vi.mocked(api.cancelPaCalibrationRun).mockResolvedValue(run({ status: 'cancelled' }));
      render(<PaCalibrationCard printerId={1} run={run({ status: 'slicing' })} />);

      await user.click(screen.getByTestId('pa-calibration-cancel'));
      await waitFor(() => expect(api.cancelPaCalibrationRun).toHaveBeenCalledWith(1, 7, false));
    });
  });

  it('shows the error a failed run recorded', () => {
    render(
      <PaCalibrationCard
        printerId={1}
        run={run({ status: 'failed', error_message: 'The printer returned no calibration result.' })}
      />,
    );
    expect(screen.getByTestId('pa-calibration-error')).toHaveTextContent(
      'The printer returned no calibration result.',
    );
  });
});
