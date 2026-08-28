import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link2, Loader2, Search, X } from 'lucide-react';
import { api } from '../api/client';
import type { InventorySpool } from '../api/client';
import { Button } from './Button';
import { useToast } from '../contexts/ToastContext';

// Link spools into a master-data group (#2936). NOT named LinkSpoolModal —
// that name is already taken twice by the Spoolman/SpoolBuddy tag-assignment
// pickers, which do something entirely different.
//
// Used from two places:
// - the inventory multi-select toolbar: candidates = the selection, and the
//   chosen source is linked with the rest of it;
// - the spool dialog ("link with existing spool"): candidates = every other
//   spool, targetIds = just the spool being edited.

interface SpoolGroupLinkModalProps {
  /** Spools that end up in the group (the source is added automatically). */
  targetIds: number[];
  /** Spools offered as the master-data source. */
  candidates: InventorySpool[];
  onClose: () => void;
  onLinked: () => void;
}

function spoolLabel(spool: InventorySpool): string {
  const parts = [spool.material, spool.subtype, spool.brand, spool.color_name].filter(Boolean);
  return `#${spool.id} ${parts.join(' · ')}`;
}

export function SpoolGroupLinkModal({ targetIds, candidates, onClose, onLinked }: SpoolGroupLinkModalProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const [sourceId, setSourceId] = useState<number | null>(null);
  const [search, setSearch] = useState('');
  const [saving, setSaving] = useState(false);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return candidates;
    return candidates.filter((spool) => spoolLabel(spool).toLowerCase().includes(q));
  }, [candidates, search]);

  // Everything that gets written: the targets minus the source itself.
  const affectedCount = new Set(targetIds.filter((id) => id !== sourceId)).size;

  const handleLink = async () => {
    if (sourceId == null) return;
    setSaving(true);
    try {
      const result = await api.linkSpools(targetIds.filter((id) => id !== sourceId), sourceId);
      showToast(t('inventory.linked.linkedToast', { count: result.updated }), 'success');
      onLinked();
      onClose();
    } catch (err) {
      console.error('SpoolGroupLinkModal.handleLink failed:', err);
      showToast(t('inventory.linked.linkFailed'), 'error');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg w-full max-w-lg border border-bambu-dark-tertiary flex flex-col max-h-[80vh]">
        <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
          <div className="flex items-center gap-2">
            <Link2 className="w-5 h-5 text-bambu-green" />
            <h2 className="text-lg font-semibold text-white">{t('inventory.linked.linkTitle')}</h2>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded hover:bg-bambu-dark text-bambu-gray hover:text-white transition-colors"
            aria-label={t('common.close')}
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-4 space-y-3 flex-1 min-h-0 flex flex-col">
          <p className="text-sm text-bambu-gray">{t('inventory.linked.pickSourceHint')}</p>

          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-bambu-gray" />
            <input
              type="text"
              className="w-full pl-10 pr-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm placeholder-bambu-gray focus:border-bambu-green focus:outline-none"
              placeholder={t('inventory.linked.searchPlaceholder')}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>

          <div className="flex-1 min-h-0 overflow-y-auto border border-bambu-dark-tertiary rounded-lg divide-y divide-bambu-dark-tertiary">
            {filtered.length === 0 ? (
              <div className="px-3 py-6 text-center text-sm text-bambu-gray">{t('inventory.linked.noCandidates')}</div>
            ) : (
              filtered.map((spool) => (
                <label
                  key={spool.id}
                  className={`flex items-center gap-3 px-3 py-2 cursor-pointer hover:bg-bambu-dark ${
                    sourceId === spool.id ? 'bg-bambu-green/10' : ''
                  }`}
                >
                  <input
                    type="radio"
                    name="link-source"
                    className="w-4 h-4 accent-bambu-green"
                    checked={sourceId === spool.id}
                    onChange={() => setSourceId(spool.id)}
                  />
                  <span className="text-sm text-white truncate">{spoolLabel(spool)}</span>
                  {spool.filament_group_id != null && (
                    <span className="ml-auto inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full bg-bambu-green/10 text-bambu-green shrink-0">
                      <Link2 className="w-3 h-3" />
                      {t('inventory.linked.alreadyLinked')}
                    </span>
                  )}
                </label>
              ))
            )}
          </div>

          {sourceId != null && (
            <p className="text-sm text-amber-500">
              {t('inventory.linked.linkConfirm', { count: affectedCount })}
            </p>
          )}
        </div>

        <div className="flex justify-end gap-2 p-4 border-t border-bambu-dark-tertiary">
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button onClick={handleLink} disabled={sourceId == null || saving}>
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Link2 className="w-4 h-4" />}
            {t('inventory.linked.linkAction')}
          </Button>
        </div>
      </div>
    </div>
  );
}
