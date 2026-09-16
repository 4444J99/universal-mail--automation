import { ClientHydration } from '../components/ClientHydration';

type LabelCount = Record<string, number>;
type LabelerStats = {
  history: LabelCount;
  total_processed?: number;
  last_run?: string;
  provider?: string;
  pending_drafts?: number;
  pending_draft_threads?: string[];
};

const VAULT_REPO = process.env.VAULT_REPO || '4444J99/estate-vault';
const VAULT_PATH = process.env.VAULT_PATH || 'universal-mail/labeler_state.json';
const GH_API_URL = `https://api.github.com/repos/${VAULT_REPO}/contents/${VAULT_PATH}`;

async function fetchVaultState(): Promise<LabelerStats | null> {
  const token = process.env.VAULT_PAT || process.env.GH_TOKEN; // allow-secret: env-var ref only, no literal secret
  if (!token) return null;

  try {
    const res = await fetch(GH_API_URL, {
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/vnd.github.v3+json',
      },
    });
    if (!res.ok) return null;
    const data = await res.json();
    const content = Buffer.from(data.content, 'base64').toString('utf8');
    return JSON.parse(content) as LabelerStats;
  } catch {
    return null;
  }
}

async function getLabelerStats() {
  const vault = await fetchVaultState();
  if (vault) return { source: 'vault' as const, data: vault };
  return { source: 'none' as const, data: null };
}

const labelToTier: Record<string, number> = {
  "Dev/GitHub": 2, "Dev/Code-Review": 2, "Dev/Infrastructure": 3, "Dev/GameDev": 3,
  "AI/Services": 3, "AI/Grok": 3, "AI/Data Exports": 2, "Finance/Banking": 1,
  "Finance/Payments": 2, "Finance/Tax": 2, "Tech/Security": 1, "Tech/Google": 2,
  "Shopping": 4, "Personal/Health": 2, "Social/LinkedIn": 3, "Travel": 2,
  "Entertainment": 4, "Education/Research": 3, "Professional/Jobs": 2,
  "Professional/Legal": 2, "Services/Domain": 2, "Notification": 3, "Marketing": 4,
  "Tech/Storage": 3, "Personal/Government": 1, "Personal": 1, "Awaiting Reply": 2,
  "Misc/Other": 4
};

const tierNames: Record<number, string> = { 1: "Critical", 2: "Important", 3: "Delegate", 4: "Reference" };

function computeTierCounts(history: LabelCount) {
  const tierCounts = { 1: 0, 2: 0, 3: 0, 4: 0 };
  for (const [label, count] of Object.entries(history)) {
    const tier = labelToTier[label] || 4;
    tierCounts[tier as keyof typeof tierCounts] += Number(count);
  }
  return tierCounts;
}

export default async function Dashboard() {
  const { source, data } = await getLabelerStats();
  const stats = data || { history: {} };
  const tierCounts = computeTierCounts(stats.history);
  const lastSync = stats.last_run ? new Date(stats.last_run).toLocaleString() : 'never';
  const sourceLabel = source === 'vault' ? 'Estate Vault (ISR)' : 'No Data';

  return (
    <main className="min-h-screen p-8 bg-gray-50 text-gray-900 font-sans">
      <div className="max-w-4xl mx-auto space-y-8">
        <header>
          <h1 className="text-3xl font-bold tracking-tight text-gray-900">Inbox Health Dashboard</h1>
          <p className="mt-2 text-gray-600">
            Overview of email processing via universal-mail-automation engine.
            <span className="ml-2 px-2 py-0.5 text-xs bg-blue-100 text-blue-700 rounded">
              Source: {sourceLabel}
            </span>
            {stats.last_run && (
              <span className="ml-2 px-2 py-0.5 text-xs bg-green-100 text-green-700 rounded">
                Last sync: {lastSync}
              </span>
            )}
            {stats.pending_drafts !== undefined && Number(stats.pending_drafts) > 0 && (
              <span className="ml-2 px-2 py-0.5 text-xs bg-red-100 text-red-700 rounded">
                {Number(stats.pending_drafts)} pending draft thread(s) — drafted but not sent
              </span>
            )}
          </p>
        </header>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          {[1, 2, 3, 4].map(tier => (
            <div key={tier} className="bg-white p-6 rounded-xl border border-gray-200 shadow-sm">
              <h3 className="text-sm font-medium text-gray-500 uppercase tracking-wider">
                Tier {tier} - {tierNames[tier]}
              </h3>
              <div className="mt-2 text-4xl font-bold text-gray-900">
                {tierCounts[tier as keyof typeof tierCounts].toLocaleString()}
              </div>
            </div>
          ))}
        </div>

        <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
          <div className="px-6 py-4 border-b border-gray-200 bg-gray-50">
            <h3 className="text-lg font-semibold text-gray-900">Label Distribution</h3>
          </div>
          <ul className="divide-y divide-gray-200">
            {Object.entries(stats.history || {})
              .sort((a, b) => Number(b[1]) - Number(a[1]))
              .map(([label, count]) => (
                <li key={label} className="px-6 py-4 flex justify-between items-center hover:bg-gray-50 transition-colors">
                  <span className="font-medium text-gray-700">{label}</span>
                  <span className="text-gray-900 font-semibold bg-gray-100 px-3 py-1 rounded-full text-sm">
                    {Number(count).toLocaleString()}
                  </span>
                </li>
              ))}
            {Object.keys(stats.history || {}).length === 0 && (
              <li className="px-6 py-8 text-center text-gray-500">
                No data available — run vault sync or labeler
              </li>
            )}
          </ul>
        </div>

        <ClientHydration source={sourceLabel} />
      </div>
    </main>
  );
}
