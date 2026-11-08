import { createReadStream } from 'node:fs';
import { realpath, stat } from 'node:fs/promises';
import { createServer } from 'node:http';
import { connect, isIPv4 } from 'node:net';
import { sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createWebSocketStream, WebSocketServer } from 'ws';

const [target, targetPort = '5900', browserPort = '6080'] = process.argv.slice(2);
const validPort = (port) => /^\d+$/.test(port) && Number(port) >= 1024 && Number(port) <= 65535;
if (!target || !isIPv4(target) || !/^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(target)
    || !validPort(targetPort) || !validPort(browserPort)) {
  console.error('Usage: npm start -- <private IPv4> [VNC port] [local browser port]');
  process.exit(2);
}

const host = `127.0.0.1:${Number(browserPort)}`;
const origin = `http://${host}`;
const novnc = await realpath(fileURLToPath(new URL('./node_modules/@novnc/novnc/', import.meta.url)));
const assets = new Map([
  ['/', ['index.html', 'text/html']],
  ['/desktop.js', ['desktop.js', 'text/javascript']],
  ['/media-lab', ['media-lab.html', 'text/html']],
  ['/media-lab.js', ['media-lab.js', 'text/javascript']],
]);
const headers = {
  'Cache-Control': 'no-store',
  'X-Content-Type-Options': 'nosniff',
  'Content-Security-Policy': `default-src 'self'; connect-src ws://${host}; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-ancestors 'none'; form-action 'none'`,
};

const server = createServer(async (request, response) => {
  if (request.headers.host !== host || request.method !== 'GET') {
    response.writeHead(403).end();
    return;
  }
  try {
    const path = new URL(request.url, origin).pathname;
    let file;
    let contentType;
    if (assets.has(path)) {
      const [name, mime] = assets.get(path);
      file = fileURLToPath(new URL(name, import.meta.url));
      contentType = mime;
    } else if (/^\/novnc\/(core|vendor)\/[a-zA-Z0-9/_-]+\.js$/.test(path)) {
      file = await realpath(novnc + path.slice('/novnc'.length));
      if (!file.startsWith(novnc + sep)) throw new Error('Asset outside the package');
      contentType = 'text/javascript';
    }
    if (!file || !(await stat(file)).isFile()) {
      response.writeHead(404).end();
      return;
    }
    response.writeHead(200, { ...headers, 'Content-Type': `${contentType}; charset=utf-8` });
    createReadStream(file).on('error', () => response.destroy()).pipe(response);
  } catch {
    response.writeHead(404).end();
  }
});

const sockets = new WebSocketServer({ noServer: true, perMessageDeflate: false, maxPayload: 1024 * 1024 });
server.on('upgrade', (request, socket, head) => {
  if (request.headers.host !== host || request.headers.origin !== origin || request.url !== '/desktop') {
    socket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n');
    return;
  }
  if (sockets.clients.size !== 0) {
    socket.end('HTTP/1.1 409 Conflict\r\nConnection: close\r\n\r\n');
    return;
  }
  sockets.handleUpgrade(request, socket, head, (websocket) => {
    const browser = createWebSocketStream(websocket);
    const desktop = connect({ host: target, port: Number(targetPort) });
    desktop.setTimeout(5000, () => desktop.destroy());
    desktop.once('connect', () => desktop.setTimeout(0));
    browser.on('error', () => desktop.destroy());
    desktop.on('error', () => browser.destroy());
    browser.on('close', () => desktop.destroy());
    desktop.on('close', () => browser.destroy());
    browser.pipe(desktop).pipe(browser);
  });
});

server.listen(Number(browserPort), '127.0.0.1', () => console.log(origin));
