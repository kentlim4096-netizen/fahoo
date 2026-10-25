// Sends each captured KR883 response / worker activity event to the tracker server (POST /activity;
// this PC, or the central one over ngrok - see config.js). If the service
// is down (PC just started, restart in progress) the event is queued in chrome.storage and retried.
importScripts("config.js");

const QUEUE_KEY = "pending";
const MAX_QUEUE = 500;

async function note(err) {
  // Last delivery error, visible in chrome.storage (and so to diagnostics) - never contains the key.
  try { await chrome.storage.local.set({ lastError: String(err).slice(0, 200), lastErrorAt: new Date().toISOString() }); } catch (e) {}
}

async function post(ev) {
  if (self.COMPUTER && !ev.computer) ev.computer = self.COMPUTER;      // which PC this came from
  const r = await fetch(self.ACTIVITY_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Activity-Key": self.ACTIVITY_KEY, "ngrok-skip-browser-warning": "1" },
    body: JSON.stringify(ev),
  });
  if (!r.ok) throw new Error("HTTP " + r.status);
  await chrome.storage.local.remove(["lastError", "lastErrorAt"]);
}

async function flush() {
  const { [QUEUE_KEY]: q = [] } = await chrome.storage.local.get(QUEUE_KEY);
  const left = [];
  for (const ev of q) {
    try { await post(ev); } catch (e) { await note(e); left.push(ev); }
  }
  await chrome.storage.local.set({ [QUEUE_KEY]: left });
}

chrome.runtime.onMessage.addListener((msg) => {
  if (!msg || msg.type !== "kr883-activity") return;
  post(msg.event).then(flush).catch(async (e) => {
    await note(e);
    const { [QUEUE_KEY]: q = [] } = await chrome.storage.local.get(QUEUE_KEY);
    q.push(msg.event);
    await chrome.storage.local.set({ [QUEUE_KEY]: q.slice(-MAX_QUEUE) });
  });
});

chrome.runtime.onStartup.addListener(flush);
chrome.alarms.create("flush", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === "flush") flush(); });
flush();                                   // also whenever the worker (re)starts, e.g. after a reload
