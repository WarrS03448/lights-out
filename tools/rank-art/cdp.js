// Minimal Chrome DevTools Protocol driver for headless Edge (no npm deps; Node 24 has WebSocket).
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

// Edge's default install path on Windows; set EDGE_PATH for anything else (a Chrome works too).
const EDGE = process.env.EDGE_PATH || "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";

async function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function launch({ port = 9333, width = 1200, height = 760 } = {}) {
  // A throwaway browser profile, outside the repository, removed again by close().
  const prof = fs.mkdtempSync(path.join(os.tmpdir(), "rank-art-edge-"));
  const proc = spawn(EDGE, [
    "--headless=new", "--remote-debugging-port=" + port, "--user-data-dir=" + prof,
    "--no-first-run", "--no-default-browser-check", "--disable-gpu", "--hide-scrollbars",
    "--disable-extensions", "--window-size=" + width + "," + height, "about:blank",
  ], { stdio: "ignore" });
  let list = null;
  for (let i = 0; i < 100 && !list; i++) {
    await sleep(100);
    try { list = await (await fetch("http://127.0.0.1:" + port + "/json/list")).json(); } catch (e) { /* not up */ }
  }
  if (!list) throw new Error("edge did not start");
  const target = list.find(t => t.type === "page");
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0;
  const pending = new Map();
  const listeners = [];
  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { res, rej } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result);
    } else if (msg.method) {
      listeners.forEach(l => l(msg));
    }
  };
  const send = (method, params = {}) => new Promise((res, rej) => {
    const i = ++id;
    pending.set(i, { res, rej });
    ws.send(JSON.stringify({ id: i, method, params }));
  });
  const logs = [];
  listeners.push(m => {
    if (m.method === "Runtime.exceptionThrown") logs.push("EXC " + JSON.stringify(m.params.exceptionDetails).slice(0, 400));
    if (m.method === "Runtime.consoleAPICalled" && (m.params.type === "error" || m.params.type === "warning"))
      logs.push(m.params.type + " " + m.params.args.map(a => a.value || a.description).join(" ").slice(0, 400));
  });
  await send("Page.enable");
  await send("Runtime.enable");
  await setSize(send, width, height);
  const api = {
    send, logs,
    async size(w, h) { await setSize(send, w, h); },
    async goto(url) {
      await send("Page.navigate", { url });
      await sleep(400);
      for (let i = 0; i < 50; i++) {
        const r = await api.eval("document.readyState");
        if (r === "complete") break;
        await sleep(100);
      }
    },
    async eval(expression) {
      const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) throw new Error("eval: " + JSON.stringify(r.exceptionDetails).slice(0, 600));
      return r.result.value;
    },
    async shot(file, clip) {
      const params = { format: "png" };
      if (clip) params.clip = { ...clip, scale: 1 };
      const r = await send("Page.captureScreenshot", params);
      fs.writeFileSync(file, Buffer.from(r.data, "base64"));
    },
    async close() {
      try { await send("Browser.close"); } catch (e) { /* gone */ }
      ws.close(); proc.kill();
      await sleep(300);
      try { fs.rmSync(prof, { recursive: true, force: true }); } catch (e) { /* still locked: tmp is fine */ }
    },
  };
  return api;
}

async function setSize(send, width, height) {
  await send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false });
}

module.exports = { launch, sleep };
