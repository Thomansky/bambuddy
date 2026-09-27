import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { Ban, BookOpen, Check, Download, Loader2, Search, X, XCircle } from 'lucide-react';
import { api } from '../../api/client';
import type { MaintenanceLogbookEntry } from '../../api/client';
import { formatDateTime, parseUTCDate, type TimeFormat } from '../../utils/date';
import { maintenanceTypeLabel } from '../../utils/maintenanceTypeLabels';
import { logbookFileName, sortableLocalTime, toCsv } from './logbookCsv';

interface MaintenanceLogbookProps {
  printers: { id: number; name: string }[];
  /** The printer the logbook is filtered to; null = all printers. */
  printerId: number | null;
  onPrinterChange: (printerId: number | null) => void;
  timeFormat?: TimeFormat;
}

type OutcomeFilter = 'all' | 'completed' | 'not_completed';

const selectClass = (active: boolean) =>
  `px-3 py-1.5 rounded-lg border text-xs font-medium transition-colors cursor-pointer focus:outline-none ${
    active
      ? 'bg-bambu-green/20 text-bambu-green border-bambu-green/30'
      : 'bg-transparent text-bambu-gray border-bambu-dark-tertiary hover:bg-bambu-dark-tertiary'
  }`;

const segmentClass = (active: boolean) =>
  `px-3 py-1.5 text-xs font-medium transition-colors ${
    active ? 'bg-bambu-green/20 text-bambu-green' : 'text-bambu-gray hover:bg-bambu-dark-tertiary'
  }`;

// The maintenance logbook: everything done on the printers, by hand or by a
// calibration run, and every calibration run that did not complete — newest
// first, filterable, and exported as a CSV for whoever asks for the record.
export function MaintenanceLogbook({ printers, printerId, onPrinterChange, timeFormat = 'system' }: MaintenanceLogbookProps) {
  const { t } = useTranslation();
  const [typeFilter, setTypeFilter] = useState('');
  const [outcome, setOutcome] = useState<OutcomeFilter>('all');
  const [search, setSearch] = useState('');
  const { data, isLoading } = useQuery({
    queryKey: ['maintenanceLogbook', printerId],
    queryFn: () => api.getMaintenanceLogbook({ printerId }),
  });

  const entries = useMemo(() => data?.entries ?? [], [data]);
  const types = useMemo(() => {
    const byId = new Map<number, string>();
    for (const entry of entries) byId.set(entry.maintenance_type_id, maintenanceTypeLabel(entry.maintenance_type_name, t));
    return [...byId.entries()].sort((a, b) => a[1].localeCompare(b[1]));
  }, [entries, t]);

  const shown = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return entries.filter((entry) => {
      if (typeFilter && String(entry.maintenance_type_id) !== typeFilter) return false;
      if (outcome === 'completed' && entry.outcome !== 'completed') return false;
      if (outcome === 'not_completed' && entry.outcome === 'completed') return false;
      if (needle) {
        const haystack = [entry.notes, entry.performed_by, entry.printer_name, maintenanceTypeLabel(entry.maintenance_type_name, t)];
        if (!haystack.some((value) => (value ?? '').toLowerCase().includes(needle))) return false;
      }
      return true;
    });
  }, [entries, typeFilter, outcome, search, t]);

  const triggerLabel = (trigger: string | null) =>
    trigger === 'schedule'
      ? t('maintenance.logbook.triggerSchedule')
      : trigger === 'due'
        ? t('maintenance.logbook.triggerDue')
        : trigger === 'manual'
          ? t('maintenance.logbook.triggerManual')
          : null;

  const kindLabel = (entry: MaintenanceLogbookEntry) => {
    if (entry.kind === 'run') {
      return [t('maintenance.logbook.kindRun'), triggerLabel(entry.source)].filter(Boolean).join(' · ');
    }
    if (entry.source === 'automatic') {
      return [t('maintenance.logbook.kindAutomatic'), triggerLabel(entry.trigger)].filter(Boolean).join(' · ');
    }
    return t('maintenance.logbook.kindManual');
  };

  const outcomeLabel = (entry: MaintenanceLogbookEntry) =>
    entry.outcome === 'completed'
      ? t('maintenance.logbook.outcomeCompleted')
      : entry.outcome === 'failed'
        ? t('maintenance.logbook.outcomeFailed')
        : t('maintenance.logbook.outcomeCancelled');

  const printerName = printerId != null ? (printers.find((p) => p.id === printerId)?.name ?? null) : null;
  // Hours in the reader's own notation, in the table and in the CSV, so a
  // German spreadsheet reads 1914,5 as a number.
  const formatHours = (hours: number) => hours.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });

  const exportCsv = () => {
    const header = [
      t('maintenance.logbook.columns.date'),
      t('maintenance.logbook.columns.printer'),
      t('maintenance.logbook.columns.maintenance'),
      t('maintenance.logbook.columns.kind'),
      t('maintenance.logbook.columns.outcome'),
      t('maintenance.logbook.columns.hours'),
      t('maintenance.logbook.columns.by'),
      t('maintenance.logbook.columns.notes'),
    ];
    const rows = shown.map((entry) => {
      const at = parseUTCDate(entry.at);
      return [
        at ? sortableLocalTime(at) : entry.at,
        entry.printer_name,
        maintenanceTypeLabel(entry.maintenance_type_name, t),
        kindLabel(entry),
        outcomeLabel(entry),
        entry.hours != null ? formatHours(entry.hours) : '',
        entry.performed_by ?? '',
        entry.notes ?? '',
      ];
    });
    const blob = new Blob([toCsv(header, rows)], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = logbookFileName(t('maintenance.logbook.fileName'), printerName, new Date());
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 100);
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={printerId ?? ''}
          onChange={(e) => onPrinterChange(e.target.value ? Number(e.target.value) : null)}
          className={selectClass(printerId != null)}
          aria-label={t('maintenance.logbook.columns.printer')}
        >
          <option value="">{t('maintenance.logbook.allPrinters')}</option>
          {printers.map((printer) => (
            <option key={printer.id} value={printer.id}>
              {printer.name}
            </option>
          ))}
        </select>
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          className={selectClass(!!typeFilter)}
          aria-label={t('maintenance.logbook.columns.maintenance')}
        >
          <option value="">{t('maintenance.logbook.allTypes')}</option>
          {types.map(([id, label]) => (
            <option key={id} value={id}>
              {label}
            </option>
          ))}
        </select>
        <div className="flex items-center rounded-lg border border-bambu-dark-tertiary overflow-hidden">
          <button onClick={() => setOutcome('all')} className={segmentClass(outcome === 'all')}>
            {t('maintenance.logbook.outcomeAll')}
          </button>
          <button onClick={() => setOutcome('completed')} className={segmentClass(outcome === 'completed')}>
            {t('maintenance.logbook.outcomeCompleted')}
          </button>
          <button onClick={() => setOutcome('not_completed')} className={segmentClass(outcome === 'not_completed')}>
            {t('maintenance.logbook.outcomeNotCompleted')}
          </button>
        </div>
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-bambu-gray/60" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('maintenance.logbook.search')}
            className="w-56 pl-8 pr-7 py-1.5 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg text-white text-xs placeholder:text-bambu-gray/50 focus:outline-none focus:border-bambu-green"
          />
          {search && (
            <button
              onClick={() => setSearch('')}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-bambu-gray hover:text-white"
              aria-label={t('common.clear')}
            >
              <X className="w-3.5 h-3.5" />
            </button>
          )}
        </div>
        <span className="ml-auto text-xs text-bambu-gray">{t('maintenance.logbook.count', { count: shown.length })}</span>
        <button
          onClick={exportCsv}
          disabled={shown.length === 0}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-bambu-gray border border-bambu-dark-tertiary rounded-lg hover:bg-bambu-dark-tertiary hover:text-white transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <Download className="w-3.5 h-3.5" />
          {t('maintenance.logbook.export')}
        </button>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-12">
          <Loader2 className="w-8 h-8 text-bambu-green animate-spin" />
        </div>
      ) : shown.length === 0 ? (
        <div className="bg-bambu-dark-secondary rounded-lg p-10 text-center space-y-2">
          <BookOpen className="w-10 h-10 text-bambu-gray/40 mx-auto" />
          <p className="text-sm text-bambu-gray">{t('maintenance.logbook.empty')}</p>
        </div>
      ) : (
        <div className="bg-bambu-dark-secondary rounded-lg overflow-hidden border border-bambu-dark-tertiary">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-bambu-dark-tertiary bg-bambu-dark-tertiary/30 text-xs text-bambu-gray uppercase tracking-wide">
                  <th className="text-left py-3 px-4 font-medium whitespace-nowrap">{t('maintenance.logbook.columns.date')}</th>
                  {printerId == null && (
                    <th className="text-left py-3 px-4 font-medium">{t('maintenance.logbook.columns.printer')}</th>
                  )}
                  <th className="text-left py-3 px-4 font-medium">{t('maintenance.logbook.columns.maintenance')}</th>
                  <th className="text-left py-3 px-4 font-medium">{t('maintenance.logbook.columns.kind')}</th>
                  <th className="text-left py-3 px-4 font-medium">{t('maintenance.logbook.columns.outcome')}</th>
                  <th className="text-right py-3 px-4 font-medium whitespace-nowrap">{t('maintenance.logbook.columns.hours')}</th>
                  <th className="text-left py-3 px-4 font-medium">{t('maintenance.logbook.columns.by')}</th>
                  <th className="text-left py-3 px-4 font-medium">{t('maintenance.logbook.columns.notes')}</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((entry) => (
                  <tr key={`${entry.kind}-${entry.id}`} className="border-b border-bambu-dark-tertiary/50 align-top">
                    <td className="py-2.5 px-4 text-white whitespace-nowrap">{formatDateTime(entry.at, timeFormat)}</td>
                    {printerId == null && <td className="py-2.5 px-4 text-white whitespace-nowrap">{entry.printer_name}</td>}
                    <td className="py-2.5 px-4 text-white">{maintenanceTypeLabel(entry.maintenance_type_name, t)}</td>
                    <td className="py-2.5 px-4 text-bambu-gray whitespace-nowrap">{kindLabel(entry)}</td>
                    <td className="py-2.5 px-4 whitespace-nowrap">
                      <span
                        className={`inline-flex items-center gap-1 text-xs ${
                          entry.outcome === 'completed'
                            ? 'text-bambu-green'
                            : entry.outcome === 'failed'
                              ? 'text-red-400'
                              : 'text-bambu-gray'
                        }`}
                      >
                        {entry.outcome === 'completed' ? (
                          <Check className="w-3.5 h-3.5" />
                        ) : entry.outcome === 'failed' ? (
                          <XCircle className="w-3.5 h-3.5" />
                        ) : (
                          <Ban className="w-3.5 h-3.5" />
                        )}
                        {outcomeLabel(entry)}
                      </span>
                    </td>
                    <td className="py-2.5 px-4 text-right text-bambu-gray whitespace-nowrap">
                      {entry.hours != null ? `${formatHours(entry.hours)} h` : '–'}
                    </td>
                    <td className="py-2.5 px-4 text-bambu-gray whitespace-nowrap">{entry.performed_by ?? '–'}</td>
                    <td className="py-2.5 px-4 text-bambu-gray max-w-md whitespace-pre-line">{entry.notes ?? ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
