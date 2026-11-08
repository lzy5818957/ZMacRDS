const byId = id => document.getElementById(id);
const form = byId('login');
const entry = byId('entry');
const connect = byId('connect');
const connectLabel = connect.querySelector('span');
const cancelConnect = byId('cancel-connect');
const status = byId('status');
const loginFeedback = byId('login-feedback');
const sessionStatus = byId('session-status');
const workspaceTools = byId('workspace-tools');
const windowCaption = byId('window-caption');
const screen = byId('screen');
const video = byId('desktop');
const sound = byId('sound');
const soundLabel = sound.querySelector('span');
const fullscreen = byId('fullscreen');
const fullscreenLabel = fullscreen.querySelector('span');
const original = byId('original');
const originalLabel = original.querySelector('span');
const disconnect = byId('disconnect');
const metrics = byId('metrics');
const measureButton = byId('measure');
const diagnostics = byId('diagnostics');
const appleLogin = byId('apple-login');
measureButton.hidden = !new URLSearchParams(location.search).has('measure');
let session;
const resumeStorageKey = 'zmacrds-desktop-resume';
const opaqueID = value => typeof value === 'string' && /^[A-Za-z0-9_-]{43}$/.test(value);
let resumeID = '';
try {
  const stored = sessionStorage.getItem(resumeStorageKey);
  if (opaqueID(stored)) resumeID = stored;
} catch {}
let retryTimer;
let retryCount = 0;

function rememberResume(id) {
  resumeID = id;
  try {
    if (id) sessionStorage.setItem(resumeStorageKey, id);
    else sessionStorage.removeItem(resumeStorageKey);
  } catch {}
}

function queueResume() {
  if (!resumeID || session || retryTimer) return;
  if (!navigator.onLine) {
    status.textContent = '网络已断开，恢复后会重新连接原桌面。';
    return;
  }
  if (retryCount >= 6) {
    status.textContent = '暂时无法恢复原桌面。可刷新重试，或重新登录。';
    return;
  }
  status.textContent = '正在重新连接原桌面…';
  retryTimer = setTimeout(() => {
    retryTimer = undefined;
    if (!session && resumeID) {
      retryCount++;
      void startDesktop({ resume: resumeID });
    }
  }, Math.min(6000, 750 * 2 ** retryCount));
}

function transportLost(current, message) {
  if (session !== current) return;
  finish(current, message);
  if (current.managed) queueResume();
}

function connectionStage(current, label, timeout, failure) {
  clearTimeout(current.deadline);
  status.textContent = label;
  current.deadline = setTimeout(() => transportLost(current, failure), timeout);
}

function showDesktop(visible) {
  if (visible) sessionStatus.append(status);
  else loginFeedback.prepend(status);
  windowCaption.hidden = visible;
  workspaceTools.hidden = !visible;
  entry.hidden = visible;
  screen.hidden = !visible;
  document.body.classList.toggle('desktop-active', visible);
}

function finish(current, message) {
  if (session !== current) return;
  session = undefined;
  current.abort.abort();
  clearInterval(current.metrics);
  clearTimeout(current.deadline);
  current.input.close();
  current.peer.close();
  video.srcObject = null;
  video.muted = true;
  showDesktop(false);
  connect.disabled = false;
  connectLabel.textContent = '登录桌面';
  cancelConnect.hidden = true;
  disconnect.disabled = sound.disabled = fullscreen.disabled = original.disabled = true;
  setOriginal(false);
  measureButton.disabled = true;
  current.stopMeasure?.();
  diagnostics.hidden = true;
  diagnostics.textContent = '';
  soundLabel.textContent = '开启声音';
  metrics.textContent = '';
  delete status.dataset.state;
  status.textContent = message;
  document.body.classList.remove('theater');
  fullscreenLabel.textContent = '全屏';
  if (document.fullscreenElement) void document.exitFullscreen().catch(() => {});
}

function gathered(peer, signal) {
  if (signal.aborted) return Promise.reject(new Error('连接已取消'));
  if (peer.iceGatheringState === 'complete') return Promise.resolve();
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      signal.removeEventListener('abort', aborted);
      peer.removeEventListener('icegatheringstatechange', changed);
    };
    const aborted = () => { cleanup(); reject(new Error('连接已取消')); };
    const changed = () => {
      if (peer.iceGatheringState === 'complete') { cleanup(); resolve(); }
    };
    signal.addEventListener('abort', aborted, { once: true });
    peer.addEventListener('icegatheringstatechange', changed);
    changed();
  });
}

async function measure(current) {
  const values = await current.peer.getStats();
  if (current !== session) return;
  const reports = Array.from(values.values());
  const picture = reports.find(row => row.type === 'inbound-rtp' && row.kind === 'video');
  const audio = reports.find(row => row.type === 'inbound-rtp' && row.kind === 'audio');
  if (!picture?.framesDecoded) return;
  const prev = current.prev;
  const priorAudio = current.audioStats;
  current.prev = picture;
  current.audioStats = audio;
  const fps = prev && picture.timestamp > prev.timestamp
    ? ((picture.framesDecoded - prev.framesDecoded) * 1000 / (picture.timestamp - prev.timestamp)).toFixed(0) : '—';
  const seconds = audio?.totalSamplesDuration - priorAudio?.totalSamplesDuration;
  const power = (audio?.totalAudioEnergy - priorAudio?.totalAudioEnergy) / seconds;
  const audioState = power > 0.000001 ? '有声音信号' : audio?.packetsReceived ? '已接收' : '等待';
  metrics.textContent = `${picture.frameWidth}×${picture.frameHeight} · 解码 ${fps} 帧/秒${current.sourceFPS === undefined ? '' : ' · 源更新 ' + current.sourceFPS + '/秒'} · 卡顿 ${picture.freezeCount ?? '—'} 次 · 音频 ${audioState}`;
}

async function startDesktop({ grant = '', resume = '' } = {}) {
  if (session) return;
  clearTimeout(retryTimer);
  retryTimer = undefined;
  if (typeof RTCPeerConnection !== 'function') {
    status.textContent = '此浏览器无法建立桌面连接，请用 Safari、Chrome 或 Edge 打开。';
    return;
  }
  const managed = Boolean(grant || resume);
  const peer = new RTCPeerConnection({ iceServers: [] });
  const input = peer.createDataChannel('desktop-input');
  const current = { peer, input, managed, abort: new AbortController(), keys: new Map(), buttons: new Map() };
  session = current;
  connect.disabled = true;
  connectLabel.textContent = '正在连接…';
  cancelConnect.hidden = false;
  disconnect.disabled = false;
  const mediaFailure = '网页服务已响应，但未能连接画面。请检查两台电脑是否在同一局域网。';
  const desktopFailure = managed ? 'Mac 没有送出桌面画面。请稍后重试，或联系管理员。'
    : '未能打开 Mac 桌面。请核对用户名和密码。新账号请先在 Mac 完成首次登录，并在 ZMacRDS 管理器中检查桌面权限。';
  const queueFailure = '桌面启动队列等待超时，请稍后重新登录。';
  const connectionLost = () => !current.mediaConnected ? mediaFailure : screen.hidden
    ? (current.waitingForDesktop ? desktopFailure : '桌面启动期间连接中断，请重新登录。')
    : '连接已断开。网络恢复后可重新登录原桌面。';
  const desktopTimeout = managed ? 200000 : 120000;
  connectionStage(current, '正在准备浏览器连接…', 6000, '浏览器准备连接超时，请重新登录。');
  peer.addTransceiver('video', { direction: 'recvonly' });
  peer.addTransceiver('audio', { direction: 'recvonly' });
  const stream = new MediaStream();
  peer.ontrack = ({ track }) => {
    if (session !== current) return;
    stream.addTrack(track);
    if (video.srcObject !== stream) video.srcObject = stream;
  };
  video.onloadeddata = () => {
    if (session !== current) return;
    clearTimeout(current.deadline);
    retryCount = 0;
    showDesktop(true);
    cancelConnect.hidden = true;
    status.dataset.state = 'connected';
    status.textContent = resume ? '桌面已恢复' : '桌面已连接';
    sound.disabled = managed;
    fullscreen.disabled = false;
    original.disabled = false;
    measureButton.disabled = measureButton.hidden;
    video.focus();
  };
  peer.onconnectionstatechange = () => {
    if (session !== current) return;
    if (peer.connectionState === 'connected' && !current.mediaConnected) {
      current.mediaConnected = true;
      current.interrupted = false;
      if (screen.hidden && !current.startupStage) connectionStage(current, '正在等待 Mac 桌面画面…', desktopTimeout, desktopFailure);
    } else if (peer.connectionState === 'disconnected') {
      current.interrupted = true;
      delete status.dataset.state;
      status.textContent = '网络暂时中断，正在等待恢复…';
    } else if (peer.connectionState === 'connected' && current.interrupted) {
      current.interrupted = false;
      release();
      if (!screen.hidden) status.dataset.state = 'connected';
      status.textContent = screen.hidden ? (current.startupStage || '正在等待 Mac 桌面画面…') : '桌面已恢复';
    } else if (['failed', 'closed'].includes(peer.connectionState)) {
      transportLost(current, connectionLost());
    }
  };
  input.onmessage = ({ data }) => {
    if (session !== current) return;
    if (screen.hidden && data === 'desktop-queued') {
      current.startupStage = '已排队，等待 Mac 启动桌面…';
      connectionStage(current, current.startupStage, 365000, queueFailure);
    }
    if (screen.hidden && data === 'desktop-starting') {
      current.waitingForDesktop = true;
      current.startupStage = '正在启动 Mac 桌面…';
      connectionStage(current, current.startupStage, desktopTimeout, desktopFailure);
    }
    if (data === 'desktop-queue-timeout') transportLost(current, queueFailure);
    if (data === 'desktop-ended') transportLost(current, screen.hidden ? desktopFailure : 'Mac 会话已结束，请重新连接。');
    const source = /^source-fps:(\d+(?:\.\d)?)$/.exec(data);
    if (source) current.sourceFPS = source[1];
  };
  input.onclose = () => transportLost(current, connectionLost());
  const user = byId('username').value;
  const password = byId('password');
  let secret = password.value;
  password.value = '';
  try {
    await peer.setLocalDescription(await peer.createOffer());
    await gathered(peer, current.abort.signal);
    if (current !== session) return;
    const sdp = peer.localDescription.sdp;
    const body = JSON.stringify(resume ? { managed: true, resume, sdp }
      : grant ? { managed: true, grant, sdp } : { username: user, password: secret, sdp });
    secret = '';
    connectionStage(current, '正在连接网页服务…', 22000, '网页服务未响应。请确认 Mac 在线并连接到同一局域网。');
    const response = await fetch('/session', {
      method: 'POST', signal: current.abort.signal,
      headers: { 'Content-Type': 'application/json' }, body,
    });
    if (session !== current) return;
    if (!response.ok) {
      if (resume && [409, 503, 504].includes(response.status)) {
        transportLost(current, '正在重新连接原桌面…');
        return;
      }
      if (resume && response.status === 401) rememberResume('');
      const message = managed && response.status === 401 ? '登录已过期，请重新登录'
        : response.status === 503 ? '连接已满，请稍后再试'
        : response.status === 429 ? '尝试次数过多，请一分钟后再试'
        : response.status === 504 ? '网页服务连接超时，请重试'
        : '连接失败，请重试';
      throw new Error(message);
    }
    const answer = await response.json();
    if (session !== current) return;
    if (managed && opaqueID(answer.resume)) rememberResume(answer.resume);
    connectionStage(current, '正在建立画面连接…', 20000, mediaFailure);
    await peer.setRemoteDescription({ type: answer.type, sdp: answer.sdp });
    if (session === current) current.metrics = setInterval(() => measure(current).catch(() => {}), 1000);
  } catch (error) {
    if (error instanceof TypeError) transportLost(current, '无法访问服务器，请检查 Mac 是否在线以及网页地址。');
    else finish(current, error.message);
  } finally {
    secret = '';
  }
}

form.addEventListener('submit', event => {
  event.preventDefault();
  void startDesktop();
});
if (location.protocol === 'https:') {
  fetch('/apple/config').then(response => response.ok && response.json()).then(settings => {
    appleLogin.hidden = !settings?.enabled;
  }).catch(() => {});
  const result = new URLSearchParams(location.search).get('apple');
  if (result === 'ready' || result === 'failed') {
    const grant = new URLSearchParams(location.hash.slice(1)).get('login');
    history.replaceState(null, '', location.pathname);
    if (result === 'ready' && opaqueID(grant)) {
      // The server replaces the old transport once the new grant is
      // authenticated. An early disconnect could lose its slot at capacity.
      rememberResume('');
      void startDesktop({ grant });
    }
    else status.textContent = '公司账号登录未完成。请用受管理的 Apple 账号重试。';
  } else if (resumeID) void startDesktop({ resume: resumeID });
}

function forgetDesktop() {
  const resume = resumeID;
  rememberResume('');
  clearTimeout(retryTimer);
  retryTimer = undefined;
  retryCount = 0;
  if (resume) void fetch('/session/end', { method: 'POST', keepalive: true,
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ resume }) }).catch(() => {});
}

function closeDesktop() {
  forgetDesktop();
  if (session) finish(session, '已断开');
}
disconnect.onclick = closeDesktop;
cancelConnect.onclick = closeDesktop;
window.addEventListener('online', () => { retryCount = 0; queueResume(); });
window.addEventListener('pageshow', event => {
  if (event.persisted && resumeID && !session) void startDesktop({ resume: resumeID });
});
sound.onclick = async () => {
  video.muted = !video.muted;
  try { await video.play(); } catch { video.muted = true; }
  soundLabel.textContent = video.muted ? '开启声音' : '静音';
  video.focus();
};
fullscreen.onclick = async () => {
  if (document.body.classList.contains('theater')) {
    document.body.classList.remove('theater');
    fullscreenLabel.textContent = '全屏';
    video.focus();
    return;
  }
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  } catch {
    document.body.classList.add('theater');
    fullscreenLabel.textContent = '退出铺满';
  }
  video.focus();
};
document.addEventListener('fullscreenchange', () => {
  fullscreenLabel.textContent = document.fullscreenElement ? '退出全屏' : '全屏';
});

function setOriginal(enabled) {
  screen.classList.toggle('original', enabled);
  original.setAttribute('aria-pressed', String(enabled));
  originalLabel.textContent = enabled ? '适应窗口' : '原始大小';
  if (!enabled) screen.scrollTo(0, 0);
}
video.addEventListener('resize', () => {
  screen.style.setProperty('--desktop-width', `${video.videoWidth}px`);
  screen.style.setProperty('--desktop-height', `${video.videoHeight}px`);
});
original.onclick = () => {
  setOriginal(!screen.classList.contains('original'));
  video.focus({ preventScroll:true });
};

measureButton.onclick = async () => {
  const current = session;
  if (!current) return;
  current.stopMeasure?.();
  const tracks = video.srcObject?.getAudioTracks();
  if (!tracks?.length || !video.requestVideoFrameCallback) return;
  const audio = new AudioContext({sampleRate:48000});
  let id, stopped = false, bright = false;
  const pulses = {video:[], audio:[]};
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 8;
  const pixels = canvas.getContext('2d', {willReadFrequently:true});
  current.stopMeasure = () => {
    stopped = true;
    if (id !== undefined) video.cancelVideoFrameCallback(id);
    if (audio.state !== 'closed') void audio.close();
  };
  diagnostics.hidden = false;
  diagnostics.textContent = '等待测试信号…';
  try {
    await audio.resume();
    await audio.audioWorklet.addModule('/audio-lab.js');
    if (stopped) return;
    const meter = new AudioWorkletNode(audio, 'calibration');
    const silent = audio.createGain();
    silent.gain.value = 0;
    audio.createMediaStreamSource(new MediaStream(tracks)).connect(meter).connect(silent).connect(audio.destination);
    let lastPowers = [-120,-120];
    const recentPowers = [];
    const toneRange = i => {
      const values = recentPowers.map(pair => pair[i]);
      return `${Math.min(...values).toFixed(1)} 至 ${Math.max(...values).toFixed(1)} dB`;
    };
    function update() {
      const samples = pulses.video.map(t => {
        const nearest = pulses.audio.reduce((best, a) => Math.abs(a-t)<Math.abs(best) ? a-t : best, Infinity);
        return Math.abs(nearest) < 700 ? Math.round(nearest) : null;
      }).filter(t => t !== null);
      diagnostics.textContent = `音频－画面：${samples.length ? samples.slice(-8).join(', ') + ' ms' : '等待配对'} · 440 Hz ${lastPowers[0].toFixed(1)} dB · 880 Hz ${lastPowers[1].toFixed(1)} dB`;
      if (recentPowers.length === 12) {
        diagnostics.textContent += ` · 最近约 1 秒：440 Hz ${toneRange(0)}，880 Hz ${toneRange(1)}`;
      }
    }
    meter.port.onmessage = ({data}) => {
      if (stopped) return;
      if (data.powers) {
        lastPowers = data.powers.map((db, i) => Math.max(db, lastPowers[i] - .15));
        recentPowers.push(data.powers);
        if (recentPowers.length > 12) recentPowers.shift();
      }
      if (data.onset !== undefined) {
        const clock = audio.getOutputTimestamp();
        if (clock.performanceTime > 0) {
          pulses.audio.push(clock.performanceTime + (data.onset-clock.contextTime)*1000);
          if (pulses.audio.length > 32) pulses.audio.shift();
        }
      }
      update();
    };
    const inspect = (_now, frame) => {
      if (stopped) return;
      pixels.drawImage(video, video.videoWidth/2-4, video.videoHeight/2-4, 8, 8, 0, 0, 8, 8);
      const rgb = pixels.getImageData(4,4,1,1).data;
      const white = rgb[0] > 220 && rgb[1] > 220 && rgb[2] > 220;
      if (white && !bright) {
        pulses.video.push(frame.expectedDisplayTime);
        if (pulses.video.length > 32) pulses.video.shift();
      }
      bright = white;
      id = video.requestVideoFrameCallback(inspect);
    };
    id = video.requestVideoFrameCallback(inspect);
  } catch {
    current.stopMeasure();
    diagnostics.textContent = '当前浏览器无法测量';
  }
};

function send(message, optional = false) {
  const current = session;
  if (!current || current.interrupted || current.input.readyState !== 'open') return false;
  if (current.input.bufferedAmount > 16384) {
    if (!optional) finish(current, '网络拥堵，已释放远程键鼠，请重新连接');
    return false;
  }
  try {
    current.input.send(JSON.stringify(message));
    return true;
  } catch { return false; }
}

function position(event) {
  const { width, height, left, top } = video.getBoundingClientRect();
  const { videoWidth, videoHeight } = video;
  if (!videoWidth || !videoHeight || !width || !height) return;
  const scale = Math.min(width / videoWidth, height / videoHeight);
  const x = Math.floor((event.clientX - left - (width - videoWidth * scale) / 2) / scale);
  const y = Math.floor((event.clientY - top - (height - videoHeight * scale) / 2) / scale);
  return { x: Math.max(0, Math.min(videoWidth - 1, x)), y: Math.max(0, Math.min(videoHeight - 1, y)) };
}
let moveQueued = false;
let nextMove;
video.addEventListener('pointermove', event => {
  nextMove = position(event);
  if (moveQueued) return;
  moveQueued = true;
  const current = session;
  requestAnimationFrame(() => {
    moveQueued = false;
    if (session === current && nextMove) send({type:'move', ...nextMove}, true);
  });
});
video.addEventListener('pointerdown', event => {
  const point = position(event);
  if (!session || !point || event.button < 0 || event.button > 2) return;
  video.focus();
  video.setPointerCapture(event.pointerId);
  const button = {type:'button', ...point, button:event.button, down:true};
  if (send(button)) session?.buttons.set(event.button, button);
  event.preventDefault();
});
video.addEventListener('pointerup', event => {
  if (!session?.buttons.delete(event.button)) return;
  send({type:'button', ...position(event), button:event.button, down:false});
  event.preventDefault();
});
video.addEventListener('contextmenu', event => event.preventDefault());
video.addEventListener('wheel', event => {
  if (event.altKey && screen.classList.contains('original')) return;
  const point = position(event);
  if (point && event.deltaY) send({type:'scroll', ...point, delta:Math.sign(event.deltaY) * -120}, true);
  event.preventDefault();
}, {passive:false});

const keyCodes = Object.fromEntries([
  'Escape Digit1 Digit2 Digit3 Digit4 Digit5 Digit6 Digit7 Digit8 Digit9 Digit0 Minus Equal Backspace Tab KeyQ KeyW KeyE KeyR KeyT KeyY KeyU KeyI KeyO KeyP BracketLeft BracketRight Enter ControlLeft KeyA KeyS KeyD KeyF KeyG KeyH KeyJ KeyK KeyL Semicolon Quote Backquote ShiftLeft Backslash KeyZ KeyX KeyC KeyV KeyB KeyN KeyM Comma Period Slash ShiftRight NumpadMultiply AltLeft Space CapsLock F1 F2 F3 F4 F5 F6 F7 F8 F9 F10'.split(' ').map((name, i) => [name, [i + 1, false]]),
  Object.entries({F11:87,F12:88}).map(([name, n]) => [name, [n,false]]),
  Object.entries({ControlRight:29,AltRight:56,Home:71,ArrowUp:72,PageUp:73,ArrowLeft:75,ArrowRight:77,End:79,ArrowDown:80,PageDown:81,Delete:83,MetaLeft:91,MetaRight:92}).map(([name,n])=>[name,[n,true]]),
].flat());
video.addEventListener('keydown', event => {
  const key = keyCodes[event.code];
  if (!key || !session) return;
  event.preventDefault();
  const message = {type:'key', code:key[0], extended:key[1], down:true};
  if (send(message)) session?.keys.set(event.code, message);
});
video.addEventListener('keyup', event => {
  const key = session?.keys.get(event.code);
  if (!key) return;
  event.preventDefault();
  session.keys.delete(event.code);
  send({...key, down:false});
});
function release() {
  const current = session;
  if (!current || current.interrupted) return;
  for (const key of current.keys.values()) send({...key, down:false});
  for (const button of current.buttons.values()) send({...button, down:false});
  current.keys.clear();
  current.buttons.clear();
}
video.addEventListener('blur', release);
video.addEventListener('pointercancel', release);
window.addEventListener('blur', release);
window.addEventListener('pagehide', () => {
  clearTimeout(retryTimer);
  retryTimer = undefined;
  if (session) finish(session, '已断开');
});
