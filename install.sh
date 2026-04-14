#!/usr/bin/env bash
# ============================================================
# VM Manager — installer for Ubuntu 22.04 / 24.04
# Usage:  sudo bash install.sh [--user <username>] [--port <port>] [--repo <url>]
# ============================================================
set -euo pipefail

# ── defaults ─────────────────────────────────────────────────────────────────
INSTALL_USER="${SUDO_USER:-$(logname 2>/dev/null || echo ubuntu)}"
APP_PORT="5000"
REPO_URL="https://github.com/zeshaq/vm-manager.git"
APP_DIR=""           # derived from INSTALL_USER below

# ── argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)  INSTALL_USER="$2"; shift 2 ;;
    --port)  APP_PORT="$2";     shift 2 ;;
    --repo)  REPO_URL="$2";     shift 2 ;;
    *)       echo "Unknown option: $1"; exit 1 ;;
  esac
done

APP_DIR="/home/${INSTALL_USER}/vm-manager"
VENV_DIR="${APP_DIR}/venv"

# ── colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()     { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
step()    { echo -e "\n${BOLD}──────────────────────────────────────${NC}"; \
            echo -e "${BOLD} $*${NC}"; \
            echo -e "${BOLD}──────────────────────────────────────${NC}"; }

# ── pre-flight checks ─────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && die "Run with sudo:  sudo bash install.sh"

step "1/9  Pre-flight checks"

# OS check
if ! grep -qiE 'ubuntu' /etc/os-release 2>/dev/null; then
  warn "This script targets Ubuntu. Proceeding anyway…"
fi

# Hardware virtualisation
VIRT_FLAGS=$(grep -cE '(vmx|svm)' /proc/cpuinfo || true)
if [[ "$VIRT_FLAGS" -eq 0 ]]; then
  warn "No vmx/svm CPU flags found. KVM will not work."
  warn "Continuing — the app will still run but cannot manage VMs."
else
  success "Hardware virtualisation detected (${VIRT_FLAGS} core(s))"
fi

# User exists?
id "$INSTALL_USER" &>/dev/null || die "User '${INSTALL_USER}' does not exist. Create it first."
success "Install target user: ${INSTALL_USER}"
info    "App directory : ${APP_DIR}"
info    "Listening port: ${APP_PORT}"
info    "Repo          : ${REPO_URL}"

# ── system packages ───────────────────────────────────────────────────────────
step "2/9  Installing system packages"

apt-get update -qq

PACKAGES=(
  # KVM / libvirt
  qemu-kvm libvirt-daemon-system libvirt-clients libvirt-dev
  bridge-utils virtinst
  # Python
  python3 python3-venv python3-pip python3-dev
  # Build deps for libvirt-python
  pkg-config gcc
  # Tools
  git curl wget
  # Kubernetes deployment support
  cloud-image-utils   # cloud-localds — build cloud-init ISOs
  qemu-utils          # qemu-img — thin-provision VM disks
  openssh-client      # ssh + ssh-keygen — cluster node access
)

apt-get install -y "${PACKAGES[@]}"
success "System packages installed"

# ── libvirt service ───────────────────────────────────────────────────────────
step "3/9  Enabling libvirt"

systemctl enable --now libvirtd
systemctl is-active --quiet libvirtd && success "libvirtd is running" \
  || warn "libvirtd did not start — check with: systemctl status libvirtd"

# Ensure default NAT network is active
virsh net-autostart default 2>/dev/null || true
virsh net-start   default 2>/dev/null || true

# ── user groups ───────────────────────────────────────────────────────────────
step "4/9  Configuring user groups"

for grp in libvirt kvm; do
  if getent group "$grp" &>/dev/null; then
    usermod -aG "$grp" "$INSTALL_USER"
    success "Added ${INSTALL_USER} to group: ${grp}"
  else
    warn "Group '${grp}' not found — skipping"
  fi
done

# ── storage directory ─────────────────────────────────────────────────────────
step "5/9  Fixing storage directory permissions"

STORAGE="/var/lib/libvirt/images"
mkdir -p "$STORAGE"
chown root:libvirt "$STORAGE"
chmod 775 "$STORAGE"
success "${STORAGE} → root:libvirt 775"

# ── Ubuntu cloud base image ───────────────────────────────────────────────────
step "6/9  Downloading Ubuntu 22.04 cloud base image"

BASE_IMAGE="${STORAGE}/ubuntu-22.04-cloudimg.img"
BASE_IMAGE_URL="https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img"

if [[ -f "$BASE_IMAGE" ]]; then
  info "Base image already exists at ${BASE_IMAGE} — skipping download"
  info "To refresh: wget -O ${BASE_IMAGE} ${BASE_IMAGE_URL}"
else
  info "Downloading Ubuntu 22.04 cloud image (~600 MB) …"
  wget -q --show-progress -O "$BASE_IMAGE" "$BASE_IMAGE_URL" \
    && success "Base image saved to ${BASE_IMAGE}" \
    || warn "Download failed — Kubernetes deployment will not work until the image is present."
fi

# ── clone / update repo ───────────────────────────────────────────────────────
step "7/9  Cloning repository"

if [[ -d "$APP_DIR/.git" ]]; then
  info "Repo already exists — pulling latest"
  sudo -u "$INSTALL_USER" git -C "$APP_DIR" pull
else
  sudo -u "$INSTALL_USER" git clone "$REPO_URL" "$APP_DIR"
fi
success "Repository ready at ${APP_DIR}"

# ── Python venv + dependencies ────────────────────────────────────────────────
step "8/9  Creating virtualenv and installing Python dependencies"

sudo -u "$INSTALL_USER" python3 -m venv "$VENV_DIR"
sudo -u "$INSTALL_USER" "$VENV_DIR/bin/pip" install --upgrade pip -q
sudo -u "$INSTALL_USER" "$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"
success "Python dependencies installed"

# ── systemd service ───────────────────────────────────────────────────────────
step "9/9  Installing systemd service"

# Generate a stable secret key
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
success "Generated SECRET_KEY"

cat > /etc/systemd/system/vm-manager.service <<SERVICE
[Unit]
Description=VM Manager
Documentation=https://github.com/zeshaq/vm-manager
After=network.target libvirtd.service
Wants=libvirtd.service

[Service]
Type=simple
User=${INSTALL_USER}
Group=${INSTALL_USER}
WorkingDirectory=${APP_DIR}
Environment="PATH=${VENV_DIR}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="PYTHONUNBUFFERED=1"
Environment="FLASK_DEBUG=false"
Environment="SECRET_KEY=${SECRET_KEY}"

ExecStart=${VENV_DIR}/bin/gunicorn \\
    --worker-class gevent \\
    --workers 1 \\
    --worker-connections 1000 \\
    --bind 0.0.0.0:${APP_PORT} \\
    --timeout 120 \\
    --keep-alive 5 \\
    --log-level info \\
    --access-logfile - \\
    --error-logfile - \\
    app:app

Restart=always
RestartSec=5
StartLimitInterval=60
StartLimitBurst=3

StandardOutput=journal
StandardError=journal
SyslogIdentifier=vm-manager

NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable vm-manager
systemctl restart vm-manager

# Wait briefly then check
sleep 4
if systemctl is-active --quiet vm-manager; then
  success "vm-manager service is running"
else
  die "Service failed to start. Check logs with: journalctl -u vm-manager -n 50"
fi

# ── summary ───────────────────────────────────────────────────────────────────
HOST_IP=$(hostname -I | awk '{print $1}')

echo ""
echo -e "${BOLD}${GREEN}════════════════════════════════════════${NC}"
echo -e "${BOLD}${GREEN}  VM Manager installed successfully!${NC}"
echo -e "${BOLD}${GREEN}════════════════════════════════════════${NC}"
echo ""
echo -e "  URL      : ${BOLD}http://${HOST_IP}:${APP_PORT}${NC}"
echo -e "  Login    : your Linux username and password"
echo -e "  Logs     : journalctl -u vm-manager -f"
echo -e "  Restart  : systemctl restart vm-manager"
echo -e "  Stop     : systemctl stop vm-manager"
echo ""
echo -e "${YELLOW}NOTE: Log out and back in (or run 'newgrp libvirt')${NC}"
echo -e "${YELLOW}      so group changes take effect for your session.${NC}"
echo ""
