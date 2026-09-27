import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Check, Loader2, X } from 'lucide-react';
import type { MaintenanceStatus } from '../../api/client';
import { maintenanceTypeLabel } from '../../utils/maintenanceTypeLabels';

interface PerformMaintenanceDialogProps {
  item: MaintenanceStatus;
  busy?: boolean;
  onConfirm: (notes: string) => void;
  onCancel: () => void;
}

// Marking a maintenance done writes a logbook entry, so it asks for the one
// thing only the person at the printer knows: what was done. The note is
// optional; who marked it is recorded without asking.
export function PerformMaintenanceDialog({ item, busy = false, onConfirm, onCancel }: PerformMaintenanceDialogProps) {
  const { t } = useTranslation();
  const [notes, setNotes] = useState('');

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onCancel]);

  const name = maintenanceTypeLabel(item.maintenance_type_name, t);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60" onClick={onCancel} />
      <div
        className="relative w-full max-w-lg mx-4 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl"
        role="dialog"
        aria-modal="true"
        aria-labelledby="perform-maintenance-title"
      >
        <div className="flex items-center justify-between gap-4 px-5 py-4 border-b border-bambu-dark-tertiary">
          <h2 id="perform-maintenance-title" className="text-lg font-semibold text-white">
            {t('maintenance.logbook.performTitle')}
          </h2>
          <button
            onClick={onCancel}
            className="p-1.5 rounded hover:bg-bambu-dark text-bambu-gray hover:text-white transition-colors"
            aria-label={t('common.close')}
          >
            <X className="w-5 h-5" />
          </button>
        </div>
        <div className="px-5 py-4 space-y-3">
          <p className="text-sm text-bambu-gray">
            {t('maintenance.logbook.performText', { name, printer: item.printer_name })}
          </p>
          <label className="block">
            <span className="text-xs text-bambu-gray">{t('maintenance.logbook.noteLabel')}</span>
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={3}
              maxLength={2000}
              autoFocus
              placeholder={t('maintenance.logbook.notePlaceholder')}
              className="w-full mt-1 px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm placeholder-bambu-gray/60 focus:border-bambu-green focus:outline-none resize-y"
            />
          </label>
        </div>
        <div className="flex justify-end gap-2 px-5 py-4 border-t border-bambu-dark-tertiary">
          <button
            onClick={onCancel}
            className="px-3 py-2 rounded-lg text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary"
          >
            {t('common.cancel')}
          </button>
          <button
            onClick={() => onConfirm(notes.trim())}
            disabled={busy}
            className="px-4 py-2 bg-bambu-green text-white rounded-lg hover:bg-bambu-green/80 flex items-center gap-1.5 disabled:opacity-50"
          >
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
            {t('maintenance.logbook.confirm')}
          </button>
        </div>
      </div>
    </div>
  );
}
