import { useEffect, useRef, useState } from 'react';
import { ApiError, request } from './api';

type Result = { status: 'no_match' } | {
  status: 'match';
  track: { video_id: string; title: string; channel: string | null; source_url: string };
};
type Session = {
  controller: AbortController;
  chunks: Blob[];
  stream?: MediaStream;
  recorder?: MediaRecorder;
  stopTimer?: ReturnType<typeof setTimeout>;
  progressTimer?: ReturnType<typeof setInterval>;
};

function releaseRecording(session: Session) {
  clearTimeout(session.stopTimer);
  clearInterval(session.progressTimer);
  if (session.recorder) {
    session.recorder.ondataavailable = null;
    session.recorder.onstop = null;
    session.recorder.onerror = null;
    if (session.recorder.state !== 'inactive') session.recorder.stop();
  }
  session.stream?.getTracks().forEach(track => track.stop());
  session.chunks.length = 0;
}

export default function IdentifySong() {
  const active = useRef<Session | null>(null);
  const [state, setState] = useState<'idle' | 'permission' | 'recording' | 'processing'>('idle');
  const [seconds, setSeconds] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Result | null>(null);

  function cancel() {
    if (active.current) {
      active.current.controller.abort();
      releaseRecording(active.current);
      active.current = null;
    }
    setState('idle');
    setSeconds(0);
  }

  useEffect(() => {
    const dispose = () => {
      if (active.current) {
        active.current.controller.abort();
        releaseRecording(active.current);
        active.current = null;
      }
    };
    const pageHide = () => { dispose(); setState('idle'); };
    window.addEventListener('pagehide', pageHide);
    return () => { window.removeEventListener('pagehide', pageHide); dispose(); };
  }, []);

  async function start() {
    if (active.current) return;
    setError(null);
    setResult(null);
    if (!window.isSecureContext) {
      setError('Microphone access needs HTTPS or localhost. Open the app through a secure address.');
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      setError('This browser cannot record microphone audio. Try a browser that supports MediaRecorder.');
      return;
    }
    const mimeType = ['audio/webm;codecs=opus', 'audio/mp4;codecs=mp4a.40.2', 'audio/mp4']
      .find(type => MediaRecorder.isTypeSupported(type));
    if (!mimeType) {
      setError('This browser cannot record WebM/Opus or MP4/AAC audio. Try another browser.');
      return;
    }
    const session: Session = { controller: new AbortController(), chunks: [] };
    active.current = session;
    setState('permission');
    setSeconds(0);

    const fail = (message: string) => {
      if (active.current !== session) return;
      session.controller.abort();
      releaseRecording(session);
      active.current = null;
      setError(message);
      setState('idle');
    };

    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      });
      if (active.current !== session) {
        stream.getTracks().forEach(track => track.stop());
        return;
      }
      session.stream = stream;
      const recorder = new MediaRecorder(stream, { mimeType });
      session.recorder = recorder;
      let byteCount = 0;
      recorder.ondataavailable = event => {
        if (active.current !== session || !event.data.size) return;
        byteCount += event.data.size;
        if (byteCount > 5 * 1024 * 1024) {
          fail('Recording exceeded 5 MiB. Try again in another browser.');
          return;
        }
        session.chunks.push(event.data);
      };
      recorder.onerror = () => fail('Microphone recording failed. Check your microphone and try again.');
      recorder.onstop = async () => {
        if (active.current !== session) return;
        let recording: Blob | null = new Blob(session.chunks, { type: mimeType });
        releaseRecording(session);
        if (!recording.size) {
          fail('Recording is empty. Check your microphone and try again.');
          return;
        }
        setState('processing');
        try {
          const next = await request<Result>('/api/identify', {
            method: 'POST', headers: { 'Content-Type': mimeType }, body: recording,
            signal: session.controller.signal,
          });
          if (active.current === session) setResult(next);
        } catch (err) {
          if (active.current === session) setError(err instanceof ApiError ? err.message : 'Could not reach the server. Check the connection and try again.');
        } finally {
          recording = null;
          session.chunks.length = 0;
          if (active.current === session) {
            active.current = null;
            setState('idle');
          }
        }
      };
      recorder.start(1000);
      const started = performance.now();
      setState('recording');
      session.progressTimer = setInterval(() => setSeconds(Math.min(10, Math.floor((performance.now() - started) / 1000))), 250);
      session.stopTimer = setTimeout(() => {
        setSeconds(10);
        if (recorder.state !== 'inactive') recorder.stop();
        stream.getTracks().forEach(track => track.stop());
      }, 10_000);
    } catch (err) {
      const name = err instanceof DOMException ? err.name : '';
      fail(name === 'NotAllowedError' ? 'Microphone permission was denied. Allow access in your browser and try again.'
        : name === 'NotFoundError' ? 'No microphone was found. Connect a microphone and try again.'
        : 'Could not start recording. Check your microphone and try again.');
    }
  }

  const busy = state !== 'idle';
  return <section className="identify-panel" aria-labelledby="identify-heading" aria-busy={busy}>
    <h2 id="identify-heading">Identify Song</h2>
    <p className="muted">Listen to about 10 seconds of music and search your indexed catalog.</p>
    <button onClick={() => void start()} disabled={busy}>Identify Song</button>
    {busy && <button className="secondary" onClick={cancel}>Cancel</button>}
    <div role="status" aria-live="polite">
      {state === 'permission' && <p>Waiting for microphone permission…</p>}
      {state === 'recording' && <>
        <p>Recording… {seconds} / 10 seconds</p>
        <progress value={seconds} max={10} aria-label="Recording progress" />
      </>}
      {state === 'processing' && <p>Searching your catalog…</p>}
      {result?.status === 'no_match' && <p>No match found</p>}
      {result?.status === 'match' && <div className="track-info match-result">
        <a href={result.track.source_url} target="_blank" rel="noreferrer">{result.track.title}</a>
        {result.track.channel && <p>{result.track.channel}</p>}
      </div>}
    </div>
    {error && <p className="error" role="alert">{error}</p>}
    <p className="muted">Your microphone recording is discarded after processing. No listening history is saved.</p>
  </section>;
}
