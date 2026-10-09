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
  audioContext?: AudioContext;
  audioSource?: MediaStreamAudioSourceNode;
  analyser?: AnalyserNode;
  animationFrame?: number;
};

function releaseRecording(session: Session) {
  clearTimeout(session.stopTimer);
  clearInterval(session.progressTimer);
  if (session.animationFrame !== undefined) cancelAnimationFrame(session.animationFrame);
  session.audioSource?.disconnect();
  session.analyser?.disconnect();
  void session.audioContext?.close();
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
  const micButton = useRef<HTMLButtonElement>(null);
  const [state, setState] = useState<'idle' | 'permission' | 'recording' | 'processing'>('idle');
  const [seconds, setSeconds] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Result | null>(null);

  useEffect(() => {
    if (state !== 'recording') micButton.current?.style.removeProperty('--audio-scale');
  }, [state]);

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
      try {
        const audioContext = new AudioContext();
        session.audioContext = audioContext;
        await audioContext.resume();
        const source = audioContext.createMediaStreamSource(stream);
        const analyser = audioContext.createAnalyser();
        analyser.fftSize = 512;
        analyser.smoothingTimeConstant = 0.3;
        source.connect(analyser);
        session.audioSource = source;
        session.analyser = analyser;
      } catch {
        // Keep recognition working if live audio visualization is unavailable.
      }
      if (active.current !== session) {
        stream.getTracks().forEach(track => track.stop());
        return;
      }
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
      if (session.analyser) {
        const frequencies = new Uint8Array(session.analyser.frequencyBinCount);
        let level = 0;
        const animate = () => {
          if (active.current !== session || !session.analyser) return;
          session.analyser.getByteFrequencyData(frequencies);
          let bass = 0;
          for (let index = 1; index < 8; index++) bass += frequencies[index];
          const target = Math.max(0, Math.min(1, (bass / 7 - 35) / 120));
          level += (target - level) * (target > level ? 0.45 : 0.16);
          micButton.current?.style.setProperty('--audio-scale', String(1 + level * 0.14));
          session.animationFrame = requestAnimationFrame(animate);
        };
        session.animationFrame = requestAnimationFrame(animate);
      }
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
  const buttonLabel = state === 'permission' ? 'Waiting for microphone permission'
    : state === 'recording' ? 'Listening for a song'
    : state === 'processing' ? 'Finding a match' : 'Identify song';

  return <section className="identify-panel" aria-labelledby="identify-heading" aria-busy={busy}>
    <h1 id="identify-heading">Identify a song</h1>
    <button ref={micButton} className={`mic-button${state === 'recording' ? ' is-recording' : ''}${busy ? ' is-busy' : ''}`}
      onClick={() => void start()} disabled={busy} aria-label={buttonLabel}>
      <svg viewBox="0 0 32 32" aria-hidden="true">
        <rect x="11" y="3" width="10" height="18" rx="5" />
        <path d="M7 15v1a9 9 0 0 0 18 0v-1M16 25v4m-5 0h10" />
      </svg>
    </button>
    <div className="identify-status" role="status" aria-live="polite">
      {state === 'idle' && !result && <p className="identify-hint">Tap to listen</p>}
      {state === 'permission' && <p className="identify-hint">Allow microphone access</p>}
      {state === 'recording' && <>
        <p className="identify-hint">Listening to nearby music</p>
        <p className="recording-time">0:{String(seconds).padStart(2, '0')} <span>/ 0:10</span></p>
        <svg className="sine-wave" viewBox="0 0 160 40" preserveAspectRatio="none" aria-hidden="true">
          <path className="sine-wave-track" d="M0 20 C10 20 10 5 20 5 S30 35 40 35 50 5 60 5 70 35 80 35 90 5 100 5 110 35 120 35 130 5 140 5 150 35 160 20" />
          <path className="sine-wave-flow" d="M0 20 C10 20 10 5 20 5 S30 35 40 35 50 5 60 5 70 35 80 35 90 5 100 5 110 35 120 35 130 5 140 5 150 35 160 20" />
        </svg>
      </>}
      {state === 'processing' && <p className="identify-hint processing-hint">Finding a match…</p>}
      {state === 'idle' && result?.status === 'no_match' && <p className="identify-hint result-message">No match found</p>}
      {state === 'idle' && result?.status === 'match' && <div className="match-result">
        <a href={result.track.source_url} target="_blank" rel="noreferrer">{result.track.title}</a>
        {result.track.channel && <p>{result.track.channel}</p>}
      </div>}
    </div>
    {busy && <button className="cancel-button" onClick={cancel}>Cancel</button>}
    {error && <p className="error identify-error" role="alert">{error}</p>}
    <p className="privacy-note">Audio is discarded after the search.</p>
  </section>;
}
