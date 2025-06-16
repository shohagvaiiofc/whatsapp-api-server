// whatsapp_api_server/index.js
const express = require('express');
const { WAProto, get)</const { WAProto, getWAConnection, DisconnectReason, useMultiFileAuthState } = require('@adiwajshing/baileys');
const { Boom } = require('@hapi/boom');
const qrcode = require('qrcode');
const pino = require('pino'); // For better logging
const fs = require('fs');
const path = require('path');

const app = express();
app.use(express.json());

const sessions = new Map(); // Store active WA sockets

// Ensure 'sessions_data' directory exists for baileys
const SESSIONS_DIR = path.join(__dirname, 'sessions_data');
if (!fs.existsSync(SESSIONS_DIR)) {
    fs.mkdirSync(SESSIONS_DIR);
}

const logger = pino({ level: 'info' }).child({ level: 'info', stream: 'store' });

async function connectToWhatsApp(phoneNumber, res) {
    const sessionPath = path.join(SESSIONS_DIR, phoneNumber);
    const { state, saveCreds } = await useMultiFileAuthState(sessionPath);

    const sock = getWAConnection({
        logger,
        printQRInTerminal: false, // We'll handle QR via HTTP
        browser: ['Termux WhatsApp Bot', 'Chrome', '1.0.0'], // Custom browser info
        auth: state,
        // Other options can be added here if needed
    });

    sessions.set(phoneNumber, sock); // Store the socket for later access

    sock.ev.on('connection.update', (update) => {
        const { connection, lastDisconnect, qr } = update;
        
        if (qr) {
            qrcode.toDataURL(qr, (err, url) => {
                if (err) {
                    logger.error("QR Code generation error:", err);
                    if (!res.headersSent) res.status(500).json({ error: 'QR Code generation error' });
                    return;
                }
                logger.info(`QR code generated for ${phoneNumber}`);
                if (!res.headersSent) res.json({ qr_url: url });
                // If the response has already been sent, don't send again.
                // This can happen if the QR updates multiple times.
            });
        }

        if (connection === 'open') {
            logger.info(`WhatsApp connection opened for ${phoneNumber}`);
            if (res && !res.headersSent) {
                // If a pending request is waiting, signal success
                res.json({ status: 'authenticated' });
            }
        }

        if (connection === 'close') {
            const shouldReconnect = (lastDisconnect.error instanceof Boom)?.output?.statusCode !== DisconnectReason.loggedOut;
            logger.info('connection closed due to ', lastDisconnect.error, ', reconnecting ', shouldReconnect);
            // Clear session from memory if logged out
            if (!shouldReconnect) {
                sessions.delete(phoneNumber);
                logger.info(`Session for ${phoneNumber} logged out and removed.`);
            }
            // You might want to automatically restart here if shouldReconnect is true
            // connectToWhatsApp(phoneNumber, null); // Reconnect without sending initial response
        }
    });

    sock.ev.on('creds.update', saveCreds);

    // Initial check for authentication status
    if (sock.user) { // If already authenticated (creds loaded from file)
        logger.info(`Already authenticated for ${phoneNumber}`);
        if (res && !res.headersSent) res.json({ status: 'authenticated' });
    }
}

app.post('/sessions', async (req, res) => {
    const { phone } = req.body;
    if (!phone) {
        return res.status(400).json({ error: 'Phone number is required' });
    }
    if (sessions.has(phone)) {
        return res.status(409).json({ error: 'Session already exists or is connecting for this phone number.' });
    }

    try {
        await connectToWhatsApp(phone, res);
    } catch (e) {
        logger.error(`Error connecting to WhatsApp for ${phone}:`, e);
        if (!res.headersSent) res.status(500).json({ error: 'Failed to initiate WhatsApp login.' });
    }
});

app.get('/sessions/:phone/status', (req, res) => {
    const { phone } = req.params;
    const sock = sessions.get(phone);

    if (sock && sock.user) {
        res.json({ status: 'authenticated' });
    } else if (sock) {
        res.json({ status: 'pending_qr' }); // QR code expected
    } else {
        res.status(404).json({ status: 'not_found' });
    }
});

app.delete('/sessions/:phone', async (req, res) => {
    const { phone } = req.params;
    const sock = sessions.get(phone);

    if (sock) {
        try {
            await sock.logout();
            sessions.delete(phone);
            // Optionally delete the session files from disk
            const sessionPath = path.join(SESSIONS_DIR, phone);
            if (fs.existsSync(sessionPath)) {
                fs.rmdirSync(sessionPath, { recursive: true });
            }
            res.json({ status: 'logged_out', message: 'Session logged out and files removed.' });
        } catch (e) {
            logger.error(`Error logging out session for ${phone}:`, e);
            res.status(500).json({ error: 'Failed to log out session.' });
        }
    } else {
        res.status(404).json({ status: 'not_found', message: 'Session not found.' });
    }
});

const PORT = 3000;
app.listen(PORT, () => console.log(`WhatsApp API running on port ${PORT}`));

// Graceful shutdown
process.on('SIGINT', async () => {
    logger.info('Shutting down WhatsApp API server...');
    for (const [phone, sock] of sessions.entries()) {
        if (sock.user) {
            await sock.logout();
            logger.info(`Logged out session for ${phone}`);
        }
    }
    process.exit(0);
});
