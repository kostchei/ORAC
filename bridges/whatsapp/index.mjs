import express from "express";
import path from "node:path";
import { fileURLToPath } from "node:url";
import Pino from "pino";
import QRCode from "qrcode";
import makeWASocket, {
  DisconnectReason,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
} from "@whiskeysockets/baileys";

const app = express();
app.use(express.json({ limit: "1mb" }));

const here = path.dirname(fileURLToPath(import.meta.url));
const root = process.env.ORAC_ROOT || path.resolve(here, "..", "..");
const authDir = path.join(root, ".orac", "whatsapp-auth");
const port = Number(process.env.ORAC_WHATSAPP_PORT || 8788);
const inbox = [];
const sentMessageIds = new Set();

let sock = null;
let connected = false;
let lastQr = null;
let lastQrAt = null;
let lastError = null;

function normalizeTarget(raw) {
  const value = String(raw || "").trim();
  if (value.includes("@")) return value;
  const digits = value.replace(/[^\d]/g, "");
  if (!digits) throw new Error("WhatsApp target must be a phone number or jid.");
  return `${digits}@s.whatsapp.net`;
}

function digitsOf(jid) {
  // Strip the "@server" suffix and any ":device" part, leaving bare digits.
  const user = (String(jid || "").split("@", 1)[0] || "").split(":", 1)[0];
  return user.replace(/[^\d]/g, "");
}

function normalizeSender(jid) {
  const digits = digitsOf(jid);
  return digits ? `+${digits}` : String(jid || "");
}

// The security boundary: ORAC only acts on the SELF-CHAT ("Message yourself"),
// the only conversation that is addressed to ORAC rather than to another person.
// A message to a friend or in a group is the operator's private conversation and
// must never be treated as an instruction — nor even ingested (it would otherwise
// land in ORAC's comms log). Fail closed: if our own identity is not yet known,
// ingest nothing.
function isSelfChat(remoteJid, ownNumber) {
  const jid = String(remoteJid || "");
  if (!jid.endsWith("@s.whatsapp.net")) return false; // groups/broadcasts never
  if (!ownNumber) return false;
  return digitsOf(jid) === ownNumber;
}

async function startSocket() {
  const { state, saveCreds } = await useMultiFileAuthState(authDir);
  const { version } = await fetchLatestBaileysVersion();
  sock = makeWASocket({
    auth: state,
    logger: Pino({ level: "silent" }),
    printQRInTerminal: true,
    version,
  });

  sock.ev.on("creds.update", saveCreds);
  sock.ev.on("connection.update", async (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      lastQr = await QRCode.toDataURL(qr);
      lastQrAt = new Date().toISOString();
      connected = false;
    }
    if (connection === "open") {
      connected = true;
      lastError = null;
      lastQr = null;
    }
    if (connection === "close") {
      connected = false;
      const code = lastDisconnect?.error?.output?.statusCode;
      lastError = lastDisconnect?.error?.message || "connection closed";
      if (code !== DisconnectReason.loggedOut) {
        setTimeout(() => startSocket().catch((err) => { lastError = String(err); }), 1500);
      }
    }
  });

  sock.ev.on("messages.upsert", ({ messages }) => {
    const ownNumber = digitsOf(sock?.user?.id);
    for (const msg of messages || []) {
      if (!msg.message || sentMessageIds.has(msg.key.id)) continue;
      // Only the self-chat drives ORAC. Drop everything else before reading its
      // text so private conversations are never buffered, logged, or acted on.
      if (!isSelfChat(msg.key.remoteJid, ownNumber)) continue;
      const text =
        msg.message.conversation ||
        msg.message.extendedTextMessage?.text ||
        msg.message.imageMessage?.caption ||
        msg.message.videoMessage?.caption ||
        "";
      if (!text.trim()) continue;
      const senderJid = msg.key.fromMe ? sock?.user?.id : (msg.key.participant || msg.key.remoteJid);
      inbox.push({
        id: msg.key.id,
        sender: normalizeSender(senderJid),
        jid: msg.key.remoteJid,
        text,
        at: new Date().toISOString(),
      });
    }
  });
}

app.get("/status", (_req, res) => {
  res.json({
    connected,
    qr: lastQr,
    qr_at: lastQrAt,
    inbox: inbox.length,
    auth_dir: authDir,
    message: connected ? "WhatsApp connected." : "Scan the QR in ORAC.",
    error: lastError,
  });
});

app.get("/qr", (_req, res) => {
  res.json({ qr: lastQr, qr_at: lastQrAt });
});

app.get("/messages", (_req, res) => {
  const messages = inbox.splice(0, inbox.length);
  res.json({ messages });
});

app.post("/send", async (req, res) => {
  try {
    if (!sock || !connected) throw new Error("WhatsApp is not connected.");
    const jid = normalizeTarget(req.body?.to);
    const text = String(req.body?.text || "");
    if (!text.trim()) throw new Error("Message text is required.");
    const sent = await sock.sendMessage(jid, { text });
    if (sent?.key?.id) sentMessageIds.add(sent.key.id);
    res.json({ ok: true });
  } catch (err) {
    res.status(400).json({ ok: false, error: String(err?.message || err) });
  }
});

app.listen(port, "127.0.0.1", () => {
  console.log(`ORAC WhatsApp bridge listening on http://127.0.0.1:${port}`);
  startSocket().catch((err) => {
    lastError = String(err);
  });
});
