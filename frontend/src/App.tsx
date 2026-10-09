import { useEffect, useState, type FormEvent } from 'react';
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
  const [playlist, setPlaylist] = useState('');
  const [connected, setConnected] = useState<boolean | null>(null);
  const [starting, setStarting] = useState(false);
  const [importId, setImportId] = useState<string | null>(null);
  const [job, setJob] = useState<ImportJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pollingFailed, setPollingFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  const [catalog, setCatalog] = useState<CatalogTrack[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [catalogRetry, setCatalogRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    request('/api/health', { signal: controller.signal }).then(() => setConnected(true)).catch(() => {
      if (!controller.signal.aborted) setConnected(false);
    });
    return () => controller.abort();
  }, []);

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
        setConnected(true);
        if (next.state === 'queued' || next.state === 'running') timer = setTimeout(poll, 1000);
      } catch (err) {
        if (controller.signal.aborted) return;
        if (err instanceof ApiError && err.status === 404) {
          setImportId(null);
          setJob(previous => previous ? { ...previous, state: 'failed', error: err.message } : null);
        } else {
          setError('Could not refresh import progress. Check the server connection and retry.');
          setPollingFailed(true);
          setConnected(false);
        }
      }
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [importId, retry]);

  useEffect(() => {
    if (job?.state === 'queued' || job?.state === 'running') return;
    const controller = new AbortController();
    setCatalogLoading(true);
    setCatalogError(null);
    request<CatalogTrack[]>('/api/catalog', { signal: controller.signal }).then(tracks => {
      if (!controller.signal.aborted) setCatalog(tracks);
    }).catch(err => {
      if (!controller.signal.aborted) setCatalogError(err instanceof ApiError ? err.message : 'Could not load your catalog. Check the connection and retry.');
    }).finally(() => {
      if (!controller.signal.aborted) setCatalogLoading(false);
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
      setConnected(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not reach the server. Check the connection and try again.');
      if (!(err instanceof ApiError)) setConnected(false);
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
    : job?.total ? `Processed ${job.completed} of ${job.total} tracks`
    : 'Reading your playlist…';

  return (
    <main>
      <header className="masthead">
        <a className="brand" href="/" aria-label="Magikazaam home">
          <svg viewBox="0 0 40 40" width="36" height="36" aria-hidden="true">
            <circle cx="20" cy="20" r="18" fill="currentColor" />
            <path d="M12 22v-4m5 10V12m6 12v-8m5 6v-4" stroke="white" strokeWidth="3" strokeLinecap="round" />
          </svg>
          Magikazaam
        </a>
        <span className={`connection ${connected === false ? 'offline' : ''}`}>
          <span className="connection-dot" />
          {connected === null ? 'Connecting…' : connected ? 'Server connected' : 'Server unavailable'}
        </span>
      </header>

      <section className="intro">
        <h1>Your playlist.<br />Ready to recognize.</h1>
        <p>Start with the songs you want Magikazaam to know. Import a YouTube playlist to build your music catalog.</p>
      </section>

      <IdentifySong />

      <section className="import-panel" aria-labelledby="import-heading">
        <div className="form-side">
          <h2 id="import-heading">Import a playlist</h2>
          <p>Use a public or unlisted playlist containing audio you’re authorized to download.</p>
          <form onSubmit={startImport}>
            <label htmlFor="playlist">YouTube playlist URL or ID</label>
            <input id="playlist" name="playlist" value={playlist} onChange={event => setPlaylist(event.target.value)}
              placeholder="https://www.youtube.com/playlist?list=…" required maxLength={2048}
              disabled={busy} aria-describedby="playlist-limits" />
            <button type="submit" disabled={busy || !playlist.trim()}>{busy ? 'Importing playlist…' : 'Import playlist'}</button>
          </form>
          <p id="playlist-limits" className="limits">Up to 100 tracks. Each track: 15 minutes and 50 MiB maximum.</p>
          {error && <div className="error" role="alert">{error}</div>}
          {pollingFailed && <button className="secondary" onClick={() => setRetry(value => value + 1)}>Retry progress</button>}
        </div>
        <aside className="note-side">
          <h3>A catalog of your own</h3>
          <p>Each available track is downloaded and checked. If a track can’t be indexed, you’ll see the reason below.</p>
          <p>Audio and fingerprints are saved in your catalog. Tracks already indexed are skipped.</p>
        </aside>
      </section>

      <section className="results" aria-labelledby="results-heading">
        <div className="results-heading">
          <h2 id="results-heading">Import results</h2>
          {job && <span>{indexed} indexed / {skipped} skipped / {failed} failed</span>}
        </div>
        {!job ? <p className="empty">Your tracks will appear here when you import a playlist.</p> : <>
          <div className="progress-area" role="status" aria-live="polite">
            <p>{stateText}</p>
            {busy && <progress max={job.total || 100} value={job.total ? job.completed : undefined} aria-label="Playlist import progress" />}
            {job.state === 'completed' && <p className="muted">{indexed + skipped ? 'Your indexed tracks are saved in the catalog below. Microphone identification comes next.' : 'No tracks were indexed. Check the errors below before trying again.'}</p>}
          </div>
          {job.error && <p className="error" role="alert">{job.error}</p>}
          <ol className="track-list">
            {job.tracks.map((track, index) => <li key={`${track.video_id ?? 'unavailable'}-${index}`}>
              <span className="track-number">{String(index + 1).padStart(2, '0')}</span>
              <div className="track-info">
                {track.source_url ? <a href={track.source_url} target="_blank" rel="noreferrer">{track.title}</a> : <strong>{track.title}</strong>}
                {track.channel && <p>{track.channel}</p>}
                {track.error && <p className="track-error">{track.error}</p>}
              </div>
              <span className={`track-state ${track.status}`}>{track.status === 'indexed' ? 'Indexed' : track.status === 'skipped' ? 'Already indexed' : 'Failed'}</span>
            </li>)}
          </ol>
        </>}
      </section>
      <section className="results" aria-labelledby="catalog-heading" aria-busy={catalogLoading}>
        <div className="results-heading">
          <h2 id="catalog-heading">Your catalog</h2>
          <span>{catalog.length} indexed {catalog.length === 1 ? 'track' : 'tracks'}</span>
        </div>
        {catalogLoading && <p className="empty" role="status">Loading your catalog…</p>}
        {catalogError && <>
          <p className="error" role="alert">{catalogError}</p>
          <button className="secondary" onClick={() => setCatalogRetry(value => value + 1)}>Retry catalog</button>
        </>}
        {!catalogLoading && !catalogError && catalog.length === 0 && <p className="empty">No indexed tracks yet. Import a playlist to add songs.</p>}
        {catalog.length > 0 && <ol className="track-list">
          {catalog.map((track, index) => <li key={track.video_id}>
            <span className="track-number">{String(index + 1).padStart(2, '0')}</span>
            <div className="track-info">
              {track.source_url ? <a href={track.source_url} target="_blank" rel="noreferrer">{track.title}</a> : <strong>{track.title}</strong>}
              {track.channel && <p>{track.channel}</p>}
            </div>
          </li>)}
        </ol>}
      </section>
      <footer>Catalog tracks are saved across restarts. Recognition thresholds use a local fixture baseline; real microphone evaluation is still pending.</footer>
    </main>
  );
}
