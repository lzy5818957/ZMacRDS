import RFB from '/novnc/core/rfb.js';

const byId = (id) => document.getElementById(id);
const status = byId('status');
const connect = byId('connect');
const disconnect = byId('disconnect');
const credentials = byId('credentials');
const username = byId('username');
const password = byId('password');
let session;
let authenticationFailed = false;

connect.addEventListener('click', () => {
  authenticationFailed = false;
  connect.disabled = true;
  disconnect.disabled = false;
  status.textContent = '正在连接 Mac…';
  session = new RFB(byId('desktop'), `ws://${location.host}/desktop`);
  session.scaleViewport = true;
  session.showDotCursor = true;
  session.addEventListener('credentialsrequired', ({ detail }) => {
    if (detail.types.some((field) => field !== 'username' && field !== 'password')) {
      status.textContent = '此 Mac 要求的登录方式暂不支持';
      authenticationFailed = true;
      session.disconnect();
      return;
    }
    username.required = detail.types.includes('username');
    credentials.hidden = false;
    (username.required ? username : password).focus();
    status.textContent = '请登录测试 Mac 账号';
  });
  session.addEventListener('connect', () => {
    status.textContent = '桌面已连接';
    credentials.hidden = true;
  });
  session.addEventListener('securityfailure', () => {
    authenticationFailed = true;
    status.textContent = 'Mac 拒绝了登录，请核对账号及屏幕共享权限';
  });
  session.addEventListener('disconnect', ({ detail }) => {
    session = undefined;
    password.value = '';
    credentials.hidden = true;
    connect.disabled = false;
    disconnect.disabled = true;
    if (!authenticationFailed) status.textContent = detail.clean ? '已断开' : '无法连接到 Mac 屏幕共享';
  });
});

credentials.addEventListener('submit', (event) => {
  event.preventDefault();
  if (!session) return;
  session.sendCredentials({ username: username.value, password: password.value });
  password.value = '';
  credentials.hidden = true;
  status.textContent = '正在验证…';
});
disconnect.addEventListener('click', () => session?.disconnect());
