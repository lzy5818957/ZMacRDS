const video = document.querySelector('#video');
const start = document.querySelector('#start');
const stop = document.querySelector('#stop');
const bitrate = document.querySelector('#bitrate');
const priority = document.querySelector('#priority');
const result = document.querySelector('#result');
let active;

function paint(context, width, height, frame) {
  context.fillStyle = '#141e30';
  context.fillRect(0, 0, width, height);
  for (let i = 0; i < 320; i++) {
    const x = ((i * 113 + frame * (3 + i % 9)) % (width + 60)) - 30;
    const y = ((i * 73 + frame * (2 + i % 5)) % (height + 44)) - 22;
    context.fillStyle = `hsl(${i * 137 % 360} 78% ${40 + i % 24}%)`;
    context.fillRect(x, y, 12 + i % 49, 8 + i % 35);
  }
  context.fillStyle = '#000b';
  context.fillRect(16, 16, 264, 56);
  context.fillStyle = '#fff';
  context.font = 'bold 30px monospace';
  context.fillText(`FRAME ${frame}`, 28, 55);
}

function cleanup(run) {
  if (active !== run) return;
  active = undefined;
  clearInterval(run.drawTimer);
  clearInterval(run.sampleTimer);
  if (run.presentationCallback !== undefined) video.cancelVideoFrameCallback(run.presentationCallback);
  for (const track of run.tracks) track.stop();
  for (const peer of run.peers) peer.close();
  run.audio?.close().catch(() => {});
  video.srcObject = null;
  start.disabled = bitrate.disabled = priority.disabled = false;
  stop.disabled = true;
}

function linkCandidates(source, target, run) {
  const pending = [];
  const add = (candidate) => target.addIceCandidate(candidate).catch((error) => {
    if (active === run) {
      result.textContent = `建立连接失败：${error.message}`;
      cleanup(run);
    }
  });
  source.onicecandidate = ({ candidate }) => {
    if (active !== run || !candidate) return;
    if (target.remoteDescription) void add(candidate);
    else pending.push(candidate);
  };
  return () => Promise.all(pending.splice(0).map(add));
}

async function sample(run, final) {
  const reports = await run.peers[1].getStats();
  if (active !== run) return;
  const values = Array.from(reports.values());
  const inbound = (kind) => values.find((item) => item.type === 'inbound-rtp' && item.kind === kind);
  const image = inbound('video');
  const sound = inbound('audio');
  const decoded = image?.framesDecoded ?? 0;
  if (decoded && !run.firstFrame) {
    run.firstFrame = performance.now();
    run.firstDecoded = decoded;
    run.firstBytes = image.bytesReceived;
  }
  const elapsed = (performance.now() - (run.firstFrame ?? performance.now())) / 1000;
  const fps = elapsed > 0 ? ((decoded - run.firstDecoded) / elapsed).toFixed(1) : '—';
  const kbps = elapsed > 0 ? Math.round((image.bytesReceived - run.firstBytes) * 8 / elapsed / 1000) : '—';
  const codec = image ? reports.get(image.codecId)?.mimeType : undefined;
  const drops = image?.framesDropped;
  const freezes = image?.freezeCount;
  const gaps = run.presentationGaps.slice().sort((a, b) => a - b);
  const gap95 = gaps[Math.ceil(gaps.length * 0.95) - 1];
  result.textContent = [
    final ? (decoded && sound?.packetsReceived ? '测试完成' : '测试结束：未收到完整音视频') : '测试中',
    `首帧呈现：${run.firstPresented === undefined ? '—' : Math.round(run.firstPresented - run.started) + ' ms'} · 帧间隔 P95：${gap95 === undefined ? '—' : Math.round(gap95) + ' ms'}`,
    `视频：${image?.frameWidth ?? '—'} × ${image?.frameHeight ?? '—'} · ${codec ?? '—'} · 平均 ${fps} 帧/秒 · ${kbps} kbps`,
    `视频帧：已解码 ${decoded} · 丢弃 ${drops ?? '—'} · 卡顿 ${freezes ?? '—'} 次`,
    `音频：${sound ? reports.get(sound.codecId)?.mimeType ?? '已接收' : '等待'} · ${sound?.packetsReceived ?? 0} 个包 · ${sound?.totalSamplesReceived ?? '—'} 个采样`,
  ].join('\n');
  if (final) cleanup(run);
}

start.addEventListener('click', async () => {
  if (active) return;
  const run = { started: performance.now(), peers: [], tracks: [], presentationGaps: [] };
  active = run;
  start.disabled = bitrate.disabled = priority.disabled = true;
  stop.disabled = false;
  result.textContent = '正在建立本机连接…';
  try {
    const canvas = document.createElement('canvas');
    canvas.width = 1280;
    canvas.height = 720;
    const context = canvas.getContext('2d', { alpha: false });
    const draw = () => paint(context, canvas.width, canvas.height, Math.floor((performance.now() - run.started) * 0.03));
    draw();
    const picture = canvas.captureStream(30).getVideoTracks()[0];
    run.tracks.push(picture);
    picture.contentHint = 'motion';
    run.drawTimer = setInterval(draw, 1000 / 30);
    run.audio = new AudioContext();
    const generator = run.audio.createOscillator();
    const gain = run.audio.createGain();
    const destination = run.audio.createMediaStreamDestination();
    gain.gain.value = 0.07;
    generator.connect(gain).connect(destination);
    generator.start();
    const sound = destination.stream.getAudioTracks()[0];
    run.tracks.push(sound);
    await run.audio.resume();
    if (active !== run) return;

    const sender = new RTCPeerConnection({ iceServers: [] });
    run.peers.push(sender);
    const receiver = new RTCPeerConnection({ iceServers: [] });
    run.peers.push(receiver);
    const sendPending = linkCandidates(sender, receiver, run);
    const receivePending = linkCandidates(receiver, sender, run);
    const output = new MediaStream();
    receiver.ontrack = ({ track }) => {
      if (active !== run) return;
      output.addTrack(track);
      if (!video.srcObject) video.srcObject = output;
    };
    if (video.requestVideoFrameCallback) {
      const watch = (now, frame) => {
        if (active !== run) return;
        const at = frame.expectedDisplayTime ?? now;
        run.firstPresented ??= at;
        if (run.lastPresented !== undefined) run.presentationGaps.push(at - run.lastPresented);
        run.lastPresented = at;
        run.presentationCallback = video.requestVideoFrameCallback(watch);
      };
      run.presentationCallback = video.requestVideoFrameCallback(watch);
    }
    const encoding = sender.addTransceiver(picture, {
      direction: 'sendonly',
      sendEncodings: [{ maxBitrate: Number(bitrate.value), maxFramerate: 30 }],
    }).sender;
    const params = encoding.getParameters();
    params.degradationPreference = priority.value;
    await encoding.setParameters(params);
    sender.addTransceiver(sound, { direction: 'sendonly' });
    await sender.setLocalDescription(await sender.createOffer());
    await receiver.setRemoteDescription(sender.localDescription);
    await sendPending();
    await receiver.setLocalDescription(await receiver.createAnswer());
    await sender.setRemoteDescription(receiver.localDescription);
    await receivePending();
    if (active !== run) return;
    let busy = false;
    run.sampleTimer = setInterval(async () => {
      if (busy) return;
      busy = true;
      try { await sample(run, performance.now() - run.started >= 12000); }
      catch (error) {
        if (active === run) {
          result.textContent = `采样失败：${error.message}`;
          cleanup(run);
        }
      } finally { busy = false; }
    }, 250);
  } catch (error) {
    if (active === run) {
      result.textContent = `测试失败：${error.message}`;
      cleanup(run);
    }
  }
});

stop.addEventListener('click', () => {
  if (!active) return;
  cleanup(active);
  result.textContent = '测试已停止';
});
window.addEventListener('pagehide', () => { if (active) cleanup(active); });
