import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Star } from 'lucide-react';
import { MAX_RATING, ratingText, ratingWord } from './productUtils';

const STARS = Array.from({ length: MAX_RATING }, (_, index) => index + 1);

interface RatingStarsProps {
  /** 1 to 4; null while not rated. */
  value: number | null;
  /** What is rated, for the screen reader ("Bambu Lab PVA"). */
  label: string;
  /** Left out, the stars only show the rating. */
  onChange?: (value: number | null) => void;
  size?: 'sm' | 'md';
}

// Four stars for how well a support material worked with a product (#3165).
// In the editor a click sets the rating and a click on the one it has clears
// it again; in the lists they only show it.
export function RatingStars({ value, label, onChange, size = 'md' }: RatingStarsProps) {
  const { t } = useTranslation();
  const [hover, setHover] = useState<number | null>(null);
  const icon = size === 'sm' ? 'w-3 h-3' : 'w-4 h-4';
  const lit = hover ?? value ?? 0;
  const star = (n: number) => (
    <Star className={`${icon} ${n <= lit ? 'text-amber-400 fill-current' : 'text-bambu-gray/40'}`} />
  );

  if (!onChange) {
    const text = ratingText(t, value);
    return (
      <span className="inline-flex items-center shrink-0" role="img" aria-label={`${label}: ${text}`} title={text}>
        {STARS.map((n) => (
          <span key={n}>{star(n)}</span>
        ))}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center shrink-0" onMouseLeave={() => setHover(null)}>
      {STARS.map((n) => (
        <button
          key={n}
          type="button"
          onClick={() => onChange(value === n ? null : n)}
          onMouseEnter={() => setHover(n)}
          aria-pressed={value === n}
          aria-label={t('inventory.products.rateAs', { name: label, rating: ratingWord(t, n) })}
          title={ratingWord(t, n)}
          className="p-0.5 rounded hover:bg-bambu-dark-tertiary/50"
        >
          {star(n)}
        </button>
      ))}
    </span>
  );
}
