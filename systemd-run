sudo tee /etc/systemd/system/vm-manager.service >/dev/null <<'EOF'
[Unit]
Description=VM Manager Flask App
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/vm-manager
Environment="PATH=/root/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="PYTHONUNBUFFERED=1"
ExecStart=/root/venv/bin/python /root/vm-manager/app.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF