// NarraFlow — Record Story
// ---------------------------------------------------------------------------
// Real microphone capture, real live transcription, real incremental story
// building. No simulated timers, no fake transcript, no hardcoded story.
//
// Wrapped in an IIFE because this file loads alongside the existing fiction
// generator's inline <script> in index.html (a second classic script, not a
// module) — top-level `const`/`let` in sibling script tags share one global
// lexical scope, so without this wrapper any name collision (there are many
// candidates: `sleep`, `logLine`, etc.) would throw and break both scripts.
(function () {
  'use strict';

  const BASE = window.location.protocol === 'file:' ? 'http://localhost:8000' : '';
  const SpeechRecognitionImpl = window.SpeechRecognition || window.webkitSpeechRecognition;

  // -------------------------------------------------------------------------
  // DOM references
  // -------------------------------------------------------------------------
  const tabNarrate = document.getElementById('tabNarrate');
  const tabFiction = document.getElementById('tabFiction');
  const narrateMode = document.getElementById('narrateMode');
  const fictionMode = document.getElementById('fictionMode');

  const subtabRecord = document.getElementById('subtabRecord');
  const subtabLibrary = document.getElementById('subtabLibrary');
  const recordView = document.getElementById('recordView');
  const libraryView = document.getElementById('libraryView');

  const recordBtn = document.getElementById('recordBtn');
  const recordBtnLabel = document.getElementById('recordBtnLabel');
  const recordStatus = document.getElementById('recordStatus');
  const recordStatusText = document.getElementById('recordStatusText');
  const recordControls = document.getElementById('recordControls');
  const recordTimer = document.getElementById('recordTimer');
  const pauseBtn = document.getElementById('pauseBtn');
  const stopBtn = document.getElementById('stopBtn');

  const narratePanels = document.getElementById('narratePanels');
  const transcriptBox = document.getElementById('transcriptBox');
  const storyBox = document.getElementById('storyBox');

  const completedStory = document.getElementById('completedStory');
  const storyTitleInput = document.getElementById('storyTitleInput');
  const completedMeta = document.getElementById('completedMeta');
  const storyAudio = document.getElementById('storyAudio');
  const completedStoryText = document.getElementById('completedStoryText');
  const completedStoryEdit = document.getElementById('completedStoryEdit');
  const editStoryBtn = document.getElementById('editStoryBtn');
  const saveStoryBtn = document.getElementById('saveStoryBtn');
  const newRecordingBtn = document.getElementById('newRecordingBtn');

  const libraryList = document.getElementById('libraryList');

  // -------------------------------------------------------------------------
  // Mode / sub-tab switching
  // -------------------------------------------------------------------------
  function showMode(mode) {
    const narrateActive = mode === 'narrate';
    tabNarrate.classList.toggle('active', narrateActive);
    tabFiction.classList.toggle('active', !narrateActive);
    tabNarrate.setAttribute('aria-selected', String(narrateActive));
    tabFiction.setAttribute('aria-selected', String(!narrateActive));
    narrateMode.classList.toggle('active', narrateActive);
    fictionMode.classList.toggle('active', !narrateActive);
  }
  tabNarrate.addEventListener('click', () => showMode('narrate'));
  tabFiction.addEventListener('click', () => showMode('fiction'));

  function showSubview(view) {
    const recordActive = view === 'record';
    subtabRecord.classList.toggle('active', recordActive);
    subtabLibrary.classList.toggle('active', !recordActive);
    subtabRecord.setAttribute('aria-selected', String(recordActive));
    subtabLibrary.setAttribute('aria-selected', String(!recordActive));
    recordView.hidden = !recordActive;
    libraryView.hidden = recordActive;
    if (!recordActive) loadLibrary();
  }
  subtabRecord.addEventListener('click', () => showSubview('record'));
  subtabLibrary.addEventListener('click', () => showSubview('library'));

  // -------------------------------------------------------------------------
  // Recording state
  // -------------------------------------------------------------------------
  // idle -> requesting -> recording <-> paused -> processing -> completed
  //                                                            \-> error
  let uiState = 'idle';
  let sessionId = null;
  let micStream = null;
  let recognizer = null;
  let sessionRecorder = null;   // continuous, whole-session audio for playback
  let fallbackRecorder = null;  // only used when SpeechRecognition is unsupported
  let fallbackTimer = null;
  let audioChunks = [];
  let intentionalRecognizerStop = false;
  let recognizerRestartAttempts = 0;

  let finalTranscript = '';
  let interimTranscript = '';
  let currentStoryText = '';

  let elapsedBeforePause = 0;
  let recordingStartedAt = 0;
  let timerHandle = null;

  const pendingSegments = [];
  let flushing = false;
  let retryHandle = null;
  let retryDelay = 2000;

  function setState(state, message) {
    uiState = state;
    recordBtn.classList.remove('recording', 'paused', 'error');
    recordStatus.classList.remove('error');
    recordBtn.disabled = false;

    switch (state) {
      case 'idle':
        recordBtnLabel.textContent = 'Start Narrating';
        recordControls.hidden = true;
        recordBtn.hidden = false;
        break;
      case 'requesting':
        recordBtnLabel.textContent = 'Requesting mic…';
        recordBtn.disabled = true;
        recordBtn.hidden = false;
        break;
      case 'recording':
        recordBtn.classList.add('recording');
        recordBtn.hidden = true;
        recordControls.hidden = false;
        pauseBtn.textContent = 'Pause';
        break;
      case 'paused':
        recordBtn.classList.add('paused');
        recordBtn.hidden = true;
        recordControls.hidden = false;
        pauseBtn.textContent = 'Resume';
        break;
      case 'processing':
        recordBtnLabel.textContent = 'Finishing up…';
        recordBtn.hidden = true;
        recordControls.hidden = true;
        break;
      case 'completed':
        recordBtn.hidden = false;
        recordBtnLabel.textContent = 'Start Narrating';
        recordControls.hidden = true;
        break;
      case 'error':
        recordBtn.classList.add('error');
        recordBtn.hidden = false;
        recordBtnLabel.textContent = 'Try Again';
        recordControls.hidden = true;
        recordStatus.classList.add('error');
        break;
      case 'interrupted':
        recordBtn.hidden = false;
        recordBtnLabel.textContent = 'Resume';
        recordControls.hidden = true;
        break;
    }
    if (message !== undefined) recordStatusText.textContent = message;
  }

  // -------------------------------------------------------------------------
  // Timer
  // -------------------------------------------------------------------------
  function formatElapsed(ms) {
    const totalSec = Math.floor(ms / 1000);
    const m = String(Math.floor(totalSec / 60)).padStart(2, '0');
    const s = String(totalSec % 60).padStart(2, '0');
    return `${m}:${s}`;
  }
  function startTimer() {
    recordingStartedAt = performance.now();
    timerHandle = setInterval(() => {
      recordTimer.textContent = formatElapsed(elapsedBeforePause + (performance.now() - recordingStartedAt));
    }, 250);
  }
  function pauseTimer() {
    elapsedBeforePause += performance.now() - recordingStartedAt;
    if (timerHandle) clearInterval(timerHandle);
  }
  function resumeTimer() { startTimer(); }
  function stopTimerAndGetSeconds() {
    if (timerHandle) clearInterval(timerHandle);
    const totalMs = elapsedBeforePause + (uiState === 'paused' ? 0 : performance.now() - recordingStartedAt);
    return totalMs / 1000;
  }
  function resetTimer() {
    elapsedBeforePause = 0;
    recordTimer.textContent = '00:00';
  }

  // -------------------------------------------------------------------------
  // Transcript + story rendering
  // -------------------------------------------------------------------------
  function renderTranscript() {
    if (!finalTranscript && !interimTranscript) {
      transcriptBox.innerHTML = '<span class="transcript-empty">Your words will appear here as you speak…</span>';
      return;
    }
    const finalHtml = finalTranscript ? `<span class="final">${escapeHtml(finalTranscript)}</span>` : '';
    const interimHtml = interimTranscript ? ` <span class="interim">${escapeHtml(interimTranscript)}</span>` : '';
    transcriptBox.innerHTML = finalHtml + interimHtml;
    transcriptBox.scrollTop = transcriptBox.scrollHeight;
  }
  function escapeHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function renderStory(text, flash) {
    currentStoryText = text || '';
    if (!currentStoryText) {
      storyBox.innerHTML = '<span class="story-empty">Your story forms here as you narrate — shaped from exactly what you say, nothing invented.</span>';
      return;
    }
    storyBox.textContent = currentStoryText;
    if (flash) {
      storyBox.classList.remove('flash');
      // eslint-disable-next-line no-unused-expressions
      void storyBox.offsetWidth; // restart the CSS animation
      storyBox.classList.add('flash');
    }
    storyBox.scrollTop = storyBox.scrollHeight;
  }

  // -------------------------------------------------------------------------
  // Segment submission — appends locally (optimistic, never blocked by the
  // network) and queues a durable POST with retry/backoff so a flaky
  // connection never loses a word the user already spoke.
  // -------------------------------------------------------------------------
  function submitSegment(text) {
    text = text.trim();
    if (!text) return;
    finalTranscript = finalTranscript ? `${finalTranscript} ${text}` : text;
    interimTranscript = '';
    renderTranscript();
    pendingSegments.push(text);
    flushSegments();
  }

  async function flushSegments() {
    if (flushing || !pendingSegments.length || !sessionId) return;
    flushing = true;
    const text = pendingSegments[0];
    try {
      const res = await fetch(`${BASE}/narration/segment`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: sessionId, text }),
      });
      if (!res.ok) throw new Error(`status ${res.status}`);
      const data = await res.json();
      pendingSegments.shift();
      retryDelay = 2000;
      if (data.story_updated) renderStory(data.story, true);
    } catch (err) {
      scheduleRetry();
    } finally {
      flushing = false;
      if (pendingSegments.length && !retryHandle) flushSegments();
    }
  }
  function scheduleRetry() {
    if (retryHandle) return;
    retryHandle = setTimeout(() => {
      retryHandle = null;
      flushSegments();
    }, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 8000);
  }

  // -------------------------------------------------------------------------
  // Mic permission + session start
  // -------------------------------------------------------------------------
  async function requestAndStart() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setState('error', "Voice narration isn't supported in this browser. Try Chrome, Edge, or Safari on a recent version.");
      return;
    }
    setState('requesting', 'Requesting microphone access…');

    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
        setState('error', 'Microphone access was denied. Enable it for this site in your browser settings, then tap Try Again.');
      } else if (err.name === 'NotFoundError' || err.name === 'DevicesNotFoundError') {
        setState('error', 'No microphone was found on this device.');
      } else {
        setState('error', `Couldn't access the microphone (${err.name || err.message}). Tap Try Again.`);
      }
      return;
    }

    let startedSessionId;
    try {
      const res = await fetch(`${BASE}/narration/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ language: 'en' }),
      });
      if (!res.ok) throw new Error(`status ${res.status}`);
      const data = await res.json();
      startedSessionId = data.id;
    } catch (err) {
      stream.getTracks().forEach((t) => t.stop());
      setState('error', "Couldn't reach NarraFlow's server to start a session. Check your connection and tap Try Again.");
      return;
    }

    sessionId = startedSessionId;
    micStream = stream;
    beginRecording(stream);
  }

  function beginRecording(stream) {
    finalTranscript = '';
    interimTranscript = '';
    currentStoryText = '';
    pendingSegments.length = 0;
    audioChunks = [];
    recognizerRestartAttempts = 0;
    resetTimer();
    renderTranscript();
    renderStory('');
    narratePanels.hidden = false;
    completedStory.hidden = true;

    // Continuous whole-session recorder, purely for the final playable file.
    sessionRecorder = new MediaRecorder(stream);
    sessionRecorder.ondataavailable = (e) => { if (e.data.size > 0) audioChunks.push(e.data); };
    sessionRecorder.start();

    if (SpeechRecognitionImpl) {
      startRecognition();
      setState('recording', 'Listening — speak naturally, your words appear below as you go.');
    } else {
      startFallbackTranscription(stream);
      setState('recording', 'Listening — your browser lacks live transcription, so text updates every few seconds instead of instantly.');
    }
    startTimer();
  }

  // -------------------------------------------------------------------------
  // Primary STT path: continuous Web Speech API with interim results.
  // -------------------------------------------------------------------------
  function startRecognition() {
    recognizer = new SpeechRecognitionImpl();
    recognizer.lang = 'en-US';
    recognizer.continuous = true;
    recognizer.interimResults = true;

    recognizer.onresult = (event) => {
      let interim = '';
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        if (result.isFinal) {
          submitSegment(result[0].transcript);
        } else {
          interim += result[0].transcript;
        }
      }
      interimTranscript = interim;
      renderTranscript();
    };

    recognizer.onerror = (event) => {
      if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
        intentionalRecognizerStop = true;
        handleInterruption('Microphone access was revoked mid-recording. Your transcript and story so far are safe.');
      }
      // 'no-speech' and other transient errors are followed by onend, handled there.
    };

    recognizer.onend = () => {
      if (intentionalRecognizerStop || uiState !== 'recording') return;
      // Chrome/most browsers stop `continuous` recognition after a period of
      // silence or a platform time limit — auto-restart so the user never
      // has to notice or re-tap anything.
      if (recognizerRestartAttempts < 3) {
        recognizerRestartAttempts++;
        try {
          recognizer.start();
          return;
        } catch (err) {
          // fall through to interruption handling below
        }
      }
      handleInterruption('Speech recognition was interrupted (often caused by the tab losing focus or the device locking). Your transcript and story so far are safe.');
    };

    intentionalRecognizerStop = false;
    recognizer.start();
  }

  function handleInterruption(message) {
    stopMediaOnly();
    setState('interrupted', `${message} Tap Resume to continue narrating — it will pick up as a new segment.`);
  }

  // -------------------------------------------------------------------------
  // Fallback STT path (no SpeechRecognition support): short-lived recorder
  // instances, restarted every ~6s, each sent whole to the existing
  // /voice/transcribe endpoint so every chunk is a complete, valid webm file.
  // -------------------------------------------------------------------------
  function startFallbackTranscription(stream) {
    const CHUNK_MS = 6000;
    const runChunk = () => {
      if (uiState !== 'recording') return;
      const chunkRecorder = new MediaRecorder(stream);
      const chunkParts = [];
      chunkRecorder.ondataavailable = (e) => { if (e.data.size > 0) chunkParts.push(e.data); };
      chunkRecorder.onstop = async () => {
        if (chunkParts.length) {
          const blob = new Blob(chunkParts, { type: 'audio/webm' });
          try {
            const form = new FormData();
            form.append('file', blob, 'chunk.webm');
            const res = await fetch(`${BASE}/voice/transcribe`, { method: 'POST', body: form });
            const data = await res.json();
            if (data.text) submitSegment(data.text);
          } catch (err) {
            // A single missed chunk isn't fatal - the next one will still arrive.
          }
        }
        fallbackRecorder = null;
        if (uiState === 'recording') fallbackTimer = setTimeout(runChunk, CHUNK_MS);
      };
      fallbackRecorder = chunkRecorder;
      chunkRecorder.start();
      fallbackTimer = setTimeout(() => { if (chunkRecorder.state !== 'inactive') chunkRecorder.stop(); }, CHUNK_MS);
    };
    runChunk();
  }

  function stopFallbackTranscription() {
    if (fallbackTimer) clearTimeout(fallbackTimer);
    fallbackTimer = null;
    if (fallbackRecorder && fallbackRecorder.state !== 'inactive') {
      try { fallbackRecorder.stop(); } catch (err) { /* already stopping */ }
    }
    fallbackRecorder = null;
  }

  // -------------------------------------------------------------------------
  // Pause / Resume / Stop
  // -------------------------------------------------------------------------
  pauseBtn.addEventListener('click', () => {
    if (uiState === 'recording') {
      intentionalRecognizerStop = true;
      if (recognizer) { try { recognizer.stop(); } catch (err) { /* noop */ } }
      stopFallbackTranscription();
      if (sessionRecorder && sessionRecorder.state === 'recording') sessionRecorder.pause();
      pauseTimer();
      setState('paused', 'Paused. Tap Resume to keep narrating — everything so far is saved.');
    } else if (uiState === 'paused') {
      intentionalRecognizerStop = false;
      recognizerRestartAttempts = 0;
      if (SpeechRecognitionImpl) startRecognition();
      else startFallbackTranscription(micStream);
      if (sessionRecorder && sessionRecorder.state === 'paused') sessionRecorder.resume();
      resumeTimer();
      setState('recording', 'Listening — speak naturally, your words appear below as you go.');
    }
  });

  recordBtn.addEventListener('click', () => {
    if (uiState === 'idle' || uiState === 'error') {
      requestAndStart();
    } else if (uiState === 'interrupted') {
      resumeAfterInterruption();
    } else if (uiState === 'completed') {
      resetToIdle();
    }
  });

  async function resumeAfterInterruption() {
    setState('requesting', 'Reconnecting the microphone…');
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      setState('error', 'Microphone access was denied. Enable it for this site in your browser settings, then tap Try Again.');
      return;
    }
    micStream = stream;
    audioChunks = []; // a short audio gap here is a known limitation of resuming after an interruption
    sessionRecorder = new MediaRecorder(stream);
    sessionRecorder.ondataavailable = (e) => { if (e.data.size > 0) audioChunks.push(e.data); };
    sessionRecorder.start();
    recognizerRestartAttempts = 0;
    intentionalRecognizerStop = false;
    if (SpeechRecognitionImpl) startRecognition();
    else startFallbackTranscription(stream);
    resumeTimer();
    setState('recording', 'Listening — speak naturally, your words appear below as you go.');
  }

  function stopMediaOnly() {
    intentionalRecognizerStop = true;
    if (recognizer) { try { recognizer.stop(); } catch (err) { /* noop */ } }
    stopFallbackTranscription();
    if (sessionRecorder && sessionRecorder.state !== 'inactive') { try { sessionRecorder.stop(); } catch (err) { /* noop */ } }
    if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
    pauseTimer();
  }

  stopBtn.addEventListener('click', async () => {
    const durationSeconds = stopTimerAndGetSeconds();
    intentionalRecognizerStop = true;
    if (recognizer) { try { recognizer.stop(); } catch (err) { /* noop */ } }
    stopFallbackTranscription();

    setState('processing', 'Finishing up — processing the last few words and finalizing your story…');

    const audioBlob = await stopSessionRecorderAndGetBlob();
    if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }

    // Let any still-in-flight segment POSTs land before forcing the final flush,
    // so the last words spoken are included in the finalized story.
    await waitForPendingSegments();

    let finalStory;
    try {
      const res = await fetch(`${BASE}/narration/stop`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: sessionId, duration_seconds: durationSeconds }),
      });
      finalStory = await res.json();
    } catch (err) {
      // The transcript and every story update up to now are already durably
      // saved server-side - only the final polish/title pass is at risk here.
      setState('error', "Couldn't reach the server to finalize your story, but everything you said so far is saved. Reopen it from My Stories once you're back online.");
      return;
    }

    if (audioBlob && audioBlob.size > 0) {
      try {
        const form = new FormData();
        form.append('file', audioBlob, 'session.webm');
        await fetch(`${BASE}/narration/audio?id=${encodeURIComponent(sessionId)}`, { method: 'POST', body: form });
        finalStory.has_audio = true;
      } catch (err) {
        // Playback just won't be available for this story - transcript/story text are unaffected.
      }
    }

    renderCompleted(finalStory);
    setState('completed', 'Your story is ready below.');
  });

  function stopSessionRecorderAndGetBlob() {
    return new Promise((resolve) => {
      if (!sessionRecorder || sessionRecorder.state === 'inactive') {
        resolve(audioChunks.length ? new Blob(audioChunks, { type: 'audio/webm' }) : null);
        return;
      }
      sessionRecorder.onstop = () => resolve(new Blob(audioChunks, { type: 'audio/webm' }));
      sessionRecorder.stop();
    });
  }

  function waitForPendingSegments() {
    // Bounded wait: under a dead connection, segments would retry forever and
    // Stop would hang in "processing" indefinitely. Give it a real chance to
    // flush, then move on - /narration/stop does its own final flush of
    // whatever the server already durably received, so this cap only risks
    // the very last still-unsent words, not anything already saved.
    const MAX_WAIT_MS = 8000;
    return new Promise((resolve) => {
      const startedAt = performance.now();
      const check = () => {
        if ((!pendingSegments.length && !flushing) || performance.now() - startedAt > MAX_WAIT_MS) resolve();
        else setTimeout(check, 200);
      };
      check();
    });
  }

  function resetToIdle() {
    sessionId = null;
    finalTranscript = '';
    interimTranscript = '';
    currentStoryText = '';
    narratePanels.hidden = true;
    completedStory.hidden = true;
    resetTimer();
    setState('idle', 'Tap to begin — NarraFlow will ask for microphone access, then transcribe and build your story as you speak.');
  }
  newRecordingBtn.addEventListener('click', resetToIdle);

  // -------------------------------------------------------------------------
  // Completed story: view, edit, playback
  // -------------------------------------------------------------------------
  function renderCompleted(story) {
    narratePanels.hidden = true;
    completedStory.hidden = false;
    storyTitleInput.value = story.title || 'Untitled Story';
    completedStoryText.textContent = story.story_text || '';
    completedStoryEdit.value = story.story_text || '';
    completedStoryEdit.hidden = true;
    completedStoryText.hidden = false;
    saveStoryBtn.hidden = true;
    editStoryBtn.hidden = false;

    const parts = [];
    if (story.duration_seconds) parts.push(formatElapsed(story.duration_seconds * 1000) + ' long');
    if (story.created_at) {
      try { parts.push(new Date(story.created_at).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })); }
      catch (err) { /* ignore malformed date */ }
    }
    parts.push(story.status || 'completed');
    completedMeta.textContent = parts.join(' · ');

    if (story.has_audio) {
      storyAudio.src = `${BASE}/narration/${story.id}/audio`;
      storyAudio.hidden = false;
    } else {
      storyAudio.removeAttribute('src');
      storyAudio.hidden = true;
    }

    completedStory.dataset.storyId = story.id;
  }

  storyTitleInput.addEventListener('blur', async () => {
    const id = completedStory.dataset.storyId;
    if (!id) return;
    const title = storyTitleInput.value.trim() || 'Untitled Story';
    try {
      await fetch(`${BASE}/narration/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title }),
      });
    } catch (err) { /* title stays as typed locally; will retry to save next edit */ }
  });

  editStoryBtn.addEventListener('click', () => {
    completedStoryEdit.value = completedStoryText.textContent;
    completedStoryText.hidden = true;
    completedStoryEdit.hidden = false;
    editStoryBtn.hidden = true;
    saveStoryBtn.hidden = false;
    completedStoryEdit.focus();
  });

  saveStoryBtn.addEventListener('click', async () => {
    const id = completedStory.dataset.storyId;
    const newText = completedStoryEdit.value;
    completedStoryText.textContent = newText;
    completedStoryText.hidden = false;
    completedStoryEdit.hidden = true;
    editStoryBtn.hidden = false;
    saveStoryBtn.hidden = true;
    if (!id) return;
    try {
      await fetch(`${BASE}/narration/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ story_text: newText }),
      });
    } catch (err) { /* edit remains visible locally even if the save call failed */ }
  });

  // -------------------------------------------------------------------------
  // Library
  // -------------------------------------------------------------------------
  async function loadLibrary() {
    libraryList.innerHTML = '<div class="empty">Loading…</div>';
    try {
      const res = await fetch(`${BASE}/narration`);
      const stories = await res.json();
      renderLibrary(stories);
    } catch (err) {
      libraryList.innerHTML = '<div class="empty">Could not load your stories — check your connection.</div>';
    }
  }

  function renderLibrary(stories) {
    if (!stories.length) {
      libraryList.innerHTML = '<div class="empty">No stories yet — narrate one and it will appear here.</div>';
      return;
    }
    libraryList.innerHTML = '';
    stories.forEach((story) => {
      const card = document.createElement('button');
      card.type = 'button';
      card.className = 'library-card';
      const date = story.created_at ? new Date(story.created_at).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : '';
      const duration = story.duration_seconds ? formatElapsed(story.duration_seconds * 1000) : '';
      card.innerHTML = `
        <span class="library-title">${escapeHtml(story.title || 'Untitled Story')}</span>
        <span class="library-meta">
          <span class="library-status ${story.status}">${story.status}</span>
          ${date ? `<span>${date}</span>` : ''}
          ${duration ? `<span>${duration}</span>` : ''}
        </span>
        <span class="library-preview">${escapeHtml(story.preview || '')}</span>
      `;
      card.addEventListener('click', () => openStory(story.id));
      libraryList.appendChild(card);
    });
  }

  async function openStory(id) {
    try {
      const res = await fetch(`${BASE}/narration/${id}`);
      if (!res.ok) throw new Error('not found');
      const story = await res.json();
      showSubview('record');
      narratePanels.hidden = true;
      renderCompleted(story);
      setState('completed', 'Your story is ready below.');
    } catch (err) {
      libraryList.insertAdjacentHTML('afterbegin', '<div class="empty">Could not open that story.</div>');
    }
  }

  // -------------------------------------------------------------------------
  // Network status — recording itself never stops for a dropped connection;
  // this only affects how quickly queued segments retry.
  // -------------------------------------------------------------------------
  window.addEventListener('online', () => {
    retryDelay = 2000;
    if (retryHandle) { clearTimeout(retryHandle); retryHandle = null; }
    flushSegments();
  });

  // -------------------------------------------------------------------------
  // Init
  // -------------------------------------------------------------------------
  setState('idle');
})();
