/* Voice AI Agent — Frontend */
'use strict';

const SESSION_ID = (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2) + Date.now().toString(36));
const WS_URL     = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/${SESSION_ID}`;

// ── State ────────────────────────────────────────────────────────────────────
let ws            = null;
let recorder      = null;
let audioContext  = null;
let analyser      = null;
let sourceNode    = null;
let animFrameId   = null;
let currentAudio  = null;
let isRecording   = false;
let isBusy        = false;

// ── DOM refs ─────────────────────────────────────────────────────────────────
const chat       = document.getElementById('chat');
const micBtn     = document.getElementById('mic-btn');
const stopBtn    = document.getElementById('stop-btn');
const clearBtn   = document.getElementById('clear-btn');
const statusLbl  = document.getElementById('status-label');
const connDot    = document.getElementById('conn-dot');
const vizCanvas  = document.getElementById('viz');
const textInput  = document.getElementById('text-input');
const textSend   = document.getElementById('text-send');
const vizCtx     = vizCanvas.getContext('2d');

// ── WebSocket ─────────────────────────────────────────────────────────────────
function connect() {
  ws = new WebSocket(WS_URL);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    connDot.className = 'status-dot';
    setStatus('Connected — ready to listen');
  };

  ws.onclose = () => {
    connDot.className = 'status-dot offline';
    setStatus('Disconnected — reconnecting...');
    setTimeout(connect, 2000);
  };

  ws.onerror = () => setStatus('Connection error');

  ws.onmessage = (evt) => {
    if (typeof evt.data === 'string') {
      handleServerMsg(JSON.parse(evt.data));
    }
  };
}

function handleServerMsg(msg) {
  switch (msg.type) {
    case 'status':
      setStatus(msg.message);
      break;

    case 'transcript':
      appendMessage('user', msg.text);
      break;

    case 'tool_call':
      appendToolBadge(`🔧 ${msg.tool}(${JSON.stringify(msg.args)})`);
      break;

    case 'tool_result':
      appendToolBadge(`✅ ${msg.tool} → ${msg.result?.substring(0, 80)}...`);
      break;

    case 'response':
      appendMessage('agent', msg.text);
      break;

    case 'audio':
      playAudioB64(msg.data);
      break;

    case 'done':
      isBusy = false;
      connDot.className = 'status-dot';
      setStatus('Ready');
      micBtn.disabled = false;
      removeThinking();
      break;

    case 'error':
      removeThinking();
      appendMessage('agent', `⚠️ Error: ${msg.message}`);
      isBusy = false;
      setStatus('Error — ready');
      micBtn.disabled = false;
      break;
  }
}

// ── Recording ─────────────────────────────────────────────────────────────────
micBtn.addEventListener('click', () => {
  if (isBusy) return;
  if (isRecording) stopRecording();
  else             startRecording();
});

async function startRecording() {
  if (!navigator.mediaDevices?.getUserMedia) {
    setStatus('Microphone not supported in this browser');
    return;
  }

  const stream = await navigator.mediaDevices.getUserMedia({ audio: { sampleRate: 16000, channelCount: 1 } });
  setupVisualizer(stream);

  const chunks = [];
  // Prefer webm/opus; fall back to any supported type
  const mimeType = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg'].find(t => MediaRecorder.isTypeSupported(t)) || '';
  recorder = new MediaRecorder(stream, mimeType ? { mimeType } : {});
  recorder.ondataavailable = e => { if (e.data.size > 0) chunks.push(e.data); };
  recorder.onstop = async () => {
    stream.getTracks().forEach(t => t.stop());
    stopVisualizer();
    const blob = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' });
    sendAudioBlob(blob);
  };

  recorder.start(100);
  isRecording = true;
  micBtn.classList.add('active');
  micBtn.textContent = '⏹';
  setStatus('Listening… click again to send');
}

function stopRecording() {
  if (recorder && recorder.state !== 'inactive') recorder.stop();
  isRecording = false;
  micBtn.classList.remove('active');
  micBtn.textContent = '🎙';
  micBtn.disabled = true;
  isBusy = true;
  connDot.className = 'status-dot busy';
  setStatus('Processing…');
  showThinking();
}

async function sendAudioBlob(blob) {
  const ab     = await blob.arrayBuffer();
  const b64    = btoa(String.fromCharCode(...new Uint8Array(ab)));
  ws.send(JSON.stringify({ type: 'audio', data: b64 }));
}

// ── Text input ────────────────────────────────────────────────────────────────
textSend.addEventListener('click', sendText);
textInput.addEventListener('keydown', e => { if (e.key === 'Enter') sendText(); });

async function sendText() {
  const text = textInput.value.trim();
  if (!text || isBusy) return;
  textInput.value = '';
  appendMessage('user', text);
  isBusy = true;
  micBtn.disabled = true;
  connDot.className = 'status-dot busy';
  setStatus('Thinking…');
  showThinking();

  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'text', text }));
    return;
  }

  // REST fallback when WebSocket is not available
  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    removeThinking();
    appendMessage('agent', data.answer);
    if (data.audio_b64) playAudioB64(data.audio_b64);
  } catch (e) {
    removeThinking();
    appendMessage('agent', `⚠️ Error: ${e.message}`);
  } finally {
    isBusy = false;
    micBtn.disabled = false;
    connDot.className = ws && ws.readyState === WebSocket.OPEN ? 'status-dot' : 'status-dot offline';
    setStatus('Ready');
  }
}

// ── Audio playback ────────────────────────────────────────────────────────────
function playAudioB64(b64) {
  if (currentAudio) { currentAudio.pause(); currentAudio = null; }
  stopBtn.style.display = 'flex';
  setStatus('Speaking…');

  const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const blob  = new Blob([bytes], { type: 'audio/mpeg' });
  const url   = URL.createObjectURL(blob);
  currentAudio = new Audio(url);
  currentAudio.onended = () => {
    URL.revokeObjectURL(url);
    stopBtn.style.display = 'none';
    currentAudio = null;
  };
  currentAudio.play();
}

stopBtn.addEventListener('click', () => {
  if (currentAudio) { currentAudio.pause(); currentAudio = null; }
  stopBtn.style.display = 'none';
});

// ── Chat UI ───────────────────────────────────────────────────────────────────
function appendMessage(role, text) {
  removeThinking();
  const wrap = document.createElement('div');
  wrap.className = `message ${role}`;
  wrap.innerHTML = `
    <div class="avatar">${role === 'user' ? '👤' : '🤖'}</div>
    <div class="bubble">
      <div class="label">${role === 'user' ? 'You' : 'Assistant'}</div>
      ${escHtml(text)}
    </div>`;
  chat.appendChild(wrap);
  scrollBottom();
}

let lastAgentBubble = null;

function appendToolBadge(text) {
  // Append tool badge to the last agent bubble if it exists, else create one
  if (!lastAgentBubble) {
    const wrap = document.createElement('div');
    wrap.className = 'message agent';
    wrap.id = 'tool-bubble';
    wrap.innerHTML = `<div class="avatar">🤖</div><div class="bubble"><div class="label">Assistant</div></div>`;
    chat.appendChild(wrap);
    lastAgentBubble = wrap.querySelector('.bubble');
  }
  const badge = document.createElement('div');
  badge.className = 'tool-badge';
  badge.textContent = text;
  lastAgentBubble.appendChild(badge);
  scrollBottom();
}

function showThinking() {
  removeThinking();
  const wrap = document.createElement('div');
  wrap.className = 'message agent';
  wrap.id = 'thinking';
  wrap.innerHTML = `
    <div class="avatar">🤖</div>
    <div class="bubble">
      <div class="dots"><span></span><span></span><span></span></div>
    </div>`;
  chat.appendChild(wrap);
  scrollBottom();
}

function removeThinking() {
  document.getElementById('thinking')?.remove();
  lastAgentBubble = null;
}

// ── Clear ─────────────────────────────────────────────────────────────────────
clearBtn.addEventListener('click', () => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'reset' }));
  chat.innerHTML = '';
  appendMessage('agent', 'Conversation cleared. How can I help you?');
});

// ── Visualizer ────────────────────────────────────────────────────────────────
function setupVisualizer(stream) {
  audioContext = new AudioContext();
  analyser     = audioContext.createAnalyser();
  analyser.fftSize = 256;
  sourceNode   = audioContext.createMediaStreamSource(stream);
  sourceNode.connect(analyser);
  drawVisualizer();
}

function drawVisualizer() {
  const W = vizCanvas.width  = vizCanvas.offsetWidth;
  const H = vizCanvas.height = vizCanvas.offsetHeight;
  const data = new Uint8Array(analyser.frequencyBinCount);

  function frame() {
    animFrameId = requestAnimationFrame(frame);
    analyser.getByteFrequencyData(data);
    vizCtx.clearRect(0, 0, W, H);

    const barW = (W / data.length) * 2.5;
    let x = 0;
    for (let i = 0; i < data.length; i++) {
      const h = (data[i] / 255) * H;
      const hue = 250 + (data[i] / 255) * 60;
      vizCtx.fillStyle = `hsl(${hue}, 70%, 60%)`;
      vizCtx.fillRect(x, H - h, barW, h);
      x += barW + 1;
    }
  }
  frame();
}

function stopVisualizer() {
  if (animFrameId) { cancelAnimationFrame(animFrameId); animFrameId = null; }
  if (audioContext) { audioContext.close(); audioContext = null; }
  vizCtx.clearRect(0, 0, vizCanvas.width, vizCanvas.height);
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function setStatus(msg) { statusLbl.textContent = msg; }
function scrollBottom() { chat.scrollTop = chat.scrollHeight; }
function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
}

// ── Init ──────────────────────────────────────────────────────────────────────
connect();
