# CreativeBot — Setup Guide

## Project Structure
```
CreativeBot/
├── app.py                  # Main Flask service
├── backup.py               # Daily DB backup script
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variable template
├── .env                    # Your actual secrets (never commit this)
├── .gitignore
├── arrivy-sync.service     # Systemd service file
├── templates/
│   ├── pin.html            # PIN lock screen
│   └── dashboard.html      # Operations dashboard
├── data/                   # SQLite database (auto-created)
└── backups/                # Daily DB backups (auto-created)
```

---

## Pi Setup

### 1. Clone the repo
```bash
cd ~
git clone https://github.com/CodyBouquet/CreativeBot.git
cd CreativeBot
```

### 2. Create virtual environment
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Set up environment variables
```bash
cp .env.example .env
nano .env
# Fill in all values — save with Ctrl+X → Y → Enter
```

### 4. Test manually
```bash
source venv/bin/activate
python app.py
# Visit http://localhost:5001 from the touchscreen
# Default PIN is 0000
```

### 5. Install as system service
```bash
sudo cp arrivy-sync.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable arrivy-sync
sudo systemctl start arrivy-sync

# Check status:
sudo systemctl status arrivy-sync

# Watch logs:
journalctl -u arrivy-sync -f
```

### 6. Set up daily backup (runs at 2am)
```bash
crontab -e
# Add this line:
0 2 * * * /home/admin/CreativeBot/venv/bin/python /home/admin/CreativeBot/backup.py >> /home/admin/CreativeBot/backup.log 2>&1
```

### 7. Allow service restart without password (needed for auto-deploy)
```bash
sudo visudo
# Add this line at the bottom:
admin ALL=(ALL) NOPASSWD: /bin/systemctl restart arrivy-sync
```

### 8. Set up Cloudflare Tunnel (once domain is ready)
```bash
# Install cloudflared (ARM64 for Pi 5):
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o cloudflared
chmod +x cloudflared
sudo mv cloudflared /usr/local/bin/

# Authenticate:
cloudflared tunnel login

# Create tunnel:
cloudflared tunnel create creativebot

# Create ~/.cloudflared/config.yml:
# tunnel: <tunnel-id>
# credentials-file: /home/admin/.cloudflared/<tunnel-id>.json
# ingress:
#   - hostname: webhook.yourdomain.com
#     service: http://localhost:5001
#   - service: http_status:404

# Route DNS:
cloudflared tunnel route dns creativebot webhook.yourdomain.com

# Install as service:
sudo cloudflared service install
sudo systemctl start cloudflared
```

### 9. Set up Chromium kiosk mode (touchscreen display)
```bash
sudo apt install --no-install-recommends xserver-xorg x11-xserver-utils xinit openbox chromium-browser -y

mkdir -p ~/.config/openbox
nano ~/.config/openbox/autostart
```
Add:
```
xset s off
xset s noblank
xset -dpms
chromium-browser --noerrdialogs --disable-infobars --kiosk http://localhost:5001
```

Then:
```bash
nano ~/.bash_profile
```
Add:
```
if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    startx
fi
```

### 10. Register webhooks

**Arrivy:**
Settings → Apps & Integrations → Webhooks → Add Webhook
URL: `https://webhook.yourdomain.com/arrivy-webhook`

**Pipedrive (deal archiving):**
Settings → Webhooks → Add Webhook
Event: `updated.deal`
URL: `https://webhook.yourdomain.com/pipedrive-webhook`

**GitHub (auto-deploy):**
Repo → Settings → Webhooks → Add webhook
Payload URL: `https://webhook.yourdomain.com/deploy`
Content type: `application/json`
Secret: (same as GITHUB_WEBHOOK_SECRET in your .env)
Event: Just the push event

---

## Default PIN
The default PIN is `0000`. Change it immediately after first login using the PIN button in the dashboard header.
