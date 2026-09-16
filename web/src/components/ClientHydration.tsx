'use client';

import { useEffect, useState, useRef } from 'react';

interface ClientHydrationProps {
  source: string;
}

export function ClientHydration({ source }: ClientHydrationProps) {
  const [hydrated, setHydrated] = useState(false);
  const [liveStats, setLiveStats] = useState<{ history: Record<string, number> } | null>(null);
  const hasHydrated = useRef(false);

  useEffect(() => {
    if (hasHydrated.current) return;
    hasHydrated.current = true;
    setHydrated(true);
    if (source === 'Estate Vault (ISR)') {
      fetch('/v1/ops/summary', { cache: 'no-store' })
        .then(r => r.ok ? r.json() : null)
        .then(data => data && setLiveStats(data))
        .catch(() => {});
    }
  }, [source]);

  if (!hydrated) return null;

  return (
    <div className="mt-8 p-4 bg-blue-50 border border-blue-200 rounded-lg">
      <p className="text-sm text-blue-700">
        Client hydrated: {liveStats ? 'Live data active' : 'Static ISR data'}
      </p>
    </div>
  );
}