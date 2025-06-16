const express = require('express');
const { WAConnection } = require('@adiwajshing/baileys');
const qrcode = require('qrcode');
const app = express();
app.use(express.json());

const sessions = {};

app.post('/sessions', async (req, res) => {
    const { phone } = req.body;
    const conn = new WAConnection();
    
    conn.on('qr', qr => {
        qrcode.toDataURL(qr, (err, url) => {
            if (err) return res.status(500).send('QR generate error');
            sessions[phone] = { conn, qr: url, authenticated: false };
            res.json({ qr_url: url });
        });
    });
    
    await conn.connect();
});

app.get('/sessions/:phone/status', (req, res) => {
    const { phone } = req.params;
    const session = sessions[phone];
    
    if (session && session.conn.authenticated) {
        res.json({
            status: 'authenticated',
            session_data: JSON.stringify(session.conn.base64EncodedAuthInfo()),
            two_fa_pass: session.conn.user.jid
        });
    } else {
        res.json({ status: 'pending' });
    }
});

app.listen(3000, () => console.log('WhatsApp API running on port 3000'));
