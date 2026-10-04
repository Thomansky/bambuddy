/**
 * A calibration run on the printer card (#3127): what runs or waits there,
 * the bed cooling down with the fans helping, and what comes next.
 */

import { describe, it, expect } from 'vitest';
import { screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { MaintenanceRunBanner } from '../../components/MaintenanceRunBanner';
import type { ActiveMaintenanceRun } from '../../api/client';

function run(patch: Partial<ActiveMaintenanceRun>): ActiveMaintenanceRun {
  return {
    id: 1,
    printer_id: 1,
    item_id: 9,
    type_name: 'Vision Encoder Calibration',
    action: 'motion_precision',
    status: 'pending',
    source: 'schedule',
    waiting_reason: null,
    waiting_detail: null,
    plate_requested_at: null,
    start_after: null,
    created_at: '2026-10-04T10:00:05Z',
    started_at: null,
    bed_temp_below: 33,
    assisted_cooling: true,
    position: 0,
    stage: null,
    ...patch,
  };
}

function serve(runs: ActiveMaintenanceRun[]) {
  server.use(http.get('/api/v1/maintenance/runs/active', () => HttpResponse.json(runs)));
}

describe('MaintenanceRunBanner', () => {
  it('shows the bed cooling down with the fans on, and that the calibration starts next', async () => {
    serve([
      run({
        waiting_reason: 'bed_too_warm',
        waiting_detail: { bed_temp: 36, threshold: 33, cooling: true },
        plate_requested_at: '2026-10-04T10:49:17Z',
      }),
    ]);
    render(<MaintenanceRunBanner printerId={1} bedTemp={35.6} />);

    expect(await screen.findByText(/Waiting: bed cooling down, 35\.6 °C, starts below 33 °C/)).toBeInTheDocument();
    expect(screen.getByText('Vision Encoder Calibration')).toBeInTheDocument();
    expect(screen.getByText('aux and exhaust fan running')).toBeInTheDocument();
    expect(screen.getByText('Next: the calibration starts by itself')).toBeInTheDocument();
  });

  it('asks for the vision encoder plate, then says the bed cools down with the fans', async () => {
    serve([run({ waiting_reason: 'vision_encoder_plate', plate_requested_at: '2026-10-04T10:51:49Z' })]);
    render(<MaintenanceRunBanner printerId={1} bedTemp={40} />);

    expect(
      await screen.findByText(/Waiting: put in the vision encoder plate, then release the plate/)
    ).toBeInTheDocument();
    expect(
      screen.getByText('Next: the bed cools down below 33 °C, helped by the aux and exhaust fan')
    ).toBeInTheDocument();
    expect(screen.queryByText('aux and exhaust fan running')).not.toBeInTheDocument();
  });

  it('names the running step, the one after it and the run waiting behind', async () => {
    serve([
      run({
        id: 2,
        type_name: 'Printer Calibration',
        action: 'calibration',
        status: 'running',
        started_at: '2026-10-04T10:00:05Z',
        stage: { index: 5, count: 8, current: 'Auto bed leveling - phase 1', next: 'Motor noise cancellation' },
      }),
      run({ id: 3, position: 1, waiting_reason: 'after_other_run', waiting_detail: { item: 'Printer Calibration' } }),
    ]);
    render(<MaintenanceRunBanner printerId={1} bedTemp={55} />);

    expect(await screen.findByText('Step 5 of 8: Auto bed leveling - phase 1')).toBeInTheDocument();
    expect(screen.getByText(/running since/)).toBeInTheDocument();
    expect(screen.getByText('Next: Motor noise cancellation')).toBeInTheDocument();
    expect(screen.getByText('After that: Vision Encoder Calibration')).toBeInTheDocument();
  });

  it('only shows the runs of its own printer', async () => {
    serve([run({ printer_id: 2, waiting_reason: 'vision_encoder_plate' })]);
    render(
      <>
        <MaintenanceRunBanner printerId={1} bedTemp={20} />
        <MaintenanceRunBanner printerId={2} bedTemp={20} />
      </>
    );

    expect(await screen.findAllByTestId('maintenance-run-banner')).toHaveLength(1);
  });
});
