import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ApiError, request } from './api';
import IdentifySong from './IdentifySong';

type Track = {
  video_id: string | null;
  title: string;
  channel: string | null;
  source_url: string | null;
  status: 'indexed' | 'skipped' | 'failed';
  error: string | null;
};

type CatalogTrack = Omit<Track, 'status' | 'error'>;

type ImportJob = {
  import_id: string;
  playlist: string;
  state: 'queued' | 'running' | 'completed' | 'failed';
  total: number;
  completed: number;
  tracks: Track[];
  error: string | null;
};

export default function App() {
  const catalogDialog = useRef<HTMLDialogElement>(null);
  const [playlist, setPlaylist] = useState('');
  const [starting, setStarting] = useState(false);
  const [importId, setImportId] = useState<string | null>(null);
  const [job, setJob] = useState<ImportJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pollingFailed, setPollingFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  const [catalog, setCatalog] = useState<CatalogTrack[]>([]);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [catalogRetry, setCatalogRetry] = useState(0);

  useEffect(() => {
    if (!importId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await request<ImportJob>(`/api/imports/${importId}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setJob(next);
        setError(null);
        setPollingFailed(false);
        if (next.state === 'queued' || next.state === 'running') timer = setTimeout(poll, 1000);
      } catch (err) {
        if (controller.signal.aborted) return;
        if (err instanceof ApiError && err.status === 404) {
          setImportId(null);
          setJob(previous => previous ? { ...previous, state: 'failed', error: err.message } : null);
        } else {
          setError('Could not refresh import progress. Check the connection and retry.');
          setPollingFailed(true);
        }
      }
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [importId, retry]);

  useEffect(() => {
    if (job?.state === 'queued' || job?.state === 'running') return;
    const controller = new AbortController();
    setCatalogError(null);
    request<CatalogTrack[]>('/api/catalog', { signal: controller.signal }).then(tracks => {
      if (!controller.signal.aborted) setCatalog(tracks);
    }).catch(err => {
      if (!controller.signal.aborted) setCatalogError(err instanceof ApiError ? err.message : 'Could not load your catalog. Check the connection and retry.');
    });
    return () => controller.abort();
  }, [job?.state, catalogRetry]);

  async function startImport(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setStarting(true);
    setError(null);
    try {
      const response = await request<{ import_id: string }>('/api/imports', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ playlist }),
      });
      setJob({ import_id: response.import_id, playlist, state: 'queued', total: 0, completed: 0, tracks: [], error: null });
      setPollingFailed(false);
      setImportId(response.import_id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not reach the server. Check the connection and try again.');
    } finally {
      setStarting(false);
    }
  }

  const busy = starting || job?.state === 'queued' || job?.state === 'running';
  const indexed = job?.tracks.filter(track => track.status === 'indexed').length ?? 0;
  const skipped = job?.tracks.filter(track => track.status === 'skipped').length ?? 0;
  const failed = job?.tracks.filter(track => track.status === 'failed').length ?? 0;
  const stateText = job?.state === 'completed' ? 'Import finished'
    : job?.state === 'failed' ? 'Import stopped'
    : job?.total ? `Importing ${job.completed} of ${job.total} tracks`
    : 'Reading your playlist…';

  return (
    <main className="app-shell">
      <header className="masthead">
        <a className="brand" href="/" aria-label="Magikazaam home">
          <svg viewBox="0 0 40 40" width="30" height="30" aria-hidden="true">
            <circle cx="20" cy="20" r="18" fill="currentColor" />
            <path d="M12 22v-4m5 10V12m6 12v-8m5 6v-4" stroke="white" strokeWidth="3" strokeLinecap="round" />
          </svg>
          <span>Magikazaam</span>
        </a>
      </header>

      <IdentifySong />

      <section className="import-section" aria-labelledby="import-heading">
        <div className="section-heading">
          <h2 id="import-heading">Import a playlist</h2>
          {catalogError ? <span className="catalog-count">Catalog unavailable</span>
            : <button className="catalog-trigger" type="button" aria-label={`View ${catalog.length} indexed ${catalog.length === 1 ? 'track' : 'tracks'}`}
              onClick={() => catalogDialog.current?.showModal()}>
              {catalog.length} {catalog.length === 1 ? 'track' : 'tracks'}
            </button>}
        </div>

        <form className="import-form" onSubmit={startImport}>
          <label className="sr-only" htmlFor="playlist">YouTube playlist URL or ID</label>
          <input id="playlist" name="playlist" value={playlist} onChange={event => setPlaylist(event.target.value)}
            placeholder="Paste a YouTube playlist URL" required maxLength={2048} disabled={busy}
            aria-describedby="authorization-note" />
          <button className="import-button" type="submit" disabled={busy || !playlist.trim()}>
            {busy ? 'Importing…' : 'Import'}
          </button>
        </form>
        <p className="authorization-note" id="authorization-note">Only import audio you’re authorized to use.</p>

        {error && <p className="error" role="alert">{error}</p>}
        {pollingFailed && <button className="text-button" onClick={() => setRetry(value => value + 1)}>Retry progress</button>}
        {catalogError && <button className="text-button" onClick={() => setCatalogRetry(value => value + 1)}>Retry catalog</button>}

        {job && <div className="import-status">
          <div className="import-summary" role="status" aria-live="polite">
            <span>{stateText}</span>
            <span className="import-counts">
              {indexed} indexed <span aria-hidden="true">·</span> {skipped} skipped <span aria-hidden="true">·</span> {failed} failed
            </span>
          </div>
          {busy && <progress max={job.total || 1} value={job.total ? job.completed : undefined} aria-label="Playlist import progress" />}
          {job.error && <p className="error" role="alert">{job.error}</p>}
          {job.tracks.length > 0 && <details className="track-details">
            <summary>View {job.tracks.length} tracks</summary>
            <ol className="track-list">
              {job.tracks.map((track, index) => <li key={`${track.video_id ?? 'unavailable'}-${index}`}>
                <div className="track-info">
                  {track.source_url ? <a href={track.source_url} target="_blank" rel="noreferrer">{track.title}</a> : <strong>{track.title}</strong>}
                  {track.channel && <span>{track.channel}</span>}
                  {track.error && <span className="track-error">{track.error}</span>}
                </div>
                <span className={`track-state ${track.status}`}>{track.status === 'indexed' ? 'Indexed' : track.status === 'skipped' ? 'Skipped' : 'Failed'}</span>
              </li>)}
            </ol>
          </details>}
        </div>}
      </section>

      <dialog ref={catalogDialog} className="catalog-dialog" aria-labelledby="catalog-dialog-heading"
        onClick={event => { if (event.target === event.currentTarget) event.currentTarget.close(); }}>
        <div className="dialog-heading">
          <div>
            <h2 id="catalog-dialog-heading">Your catalog</h2>
            <p>{catalog.length} indexed {catalog.length === 1 ? 'track' : 'tracks'}</p>
          </div>
          <form method="dialog"><button className="dialog-close" aria-label="Close catalog">×</button></form>
        </div>
        {catalog.length ? <ol className="catalog-list">
          {catalog.map(track => <li key={track.video_id}>
            <div className="track-info">
              {track.source_url ? <a href={track.source_url} target="_blank" rel="noreferrer">{track.title}</a> : <strong>{track.title}</strong>}
              {track.channel && <span>{track.channel}</span>}
            </div>
          </li>)}
        </ol> : <p className="catalog-empty">No indexed tracks yet.</p>}
      </dialog>
    </main>
  );
}
