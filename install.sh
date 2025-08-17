#!/bin/bash

# Lightning Detector v2.1 Installation Script for Raspberry Pi
# This script must be run as root
# Tested on Raspberry Pi OS (Debian Bullseye/Bookworm)

set -e  # Exit on error

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${GREEN}[✓]${NC} $1"
}

print_error() {
    echo -e "${RED}[✗]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[!]${NC} $1"
}

print_info() {
    echo -e "${BLUE}[i]${NC} $1"
}

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   print_error "This script must be run as root"
   echo "Please run: sudo bash install_lightning_detector.sh"
   exit 1
fi

# Banner
echo "=================================================="
echo "Lightning Detector v2.1 Installation Script"
echo "For Raspberry Pi with AS3935 Sensor"
echo "=================================================="
echo ""

# Get installation directory
read -p "Enter installation directory [/opt/lightning-detector]: " INSTALL_DIR
INSTALL_DIR=${INSTALL_DIR:-/opt/lightning-detector}

print_info "Installation directory: $INSTALL_DIR"
echo ""

# Update system
print_status "Updating system packages..."
apt-get update
apt-get upgrade -y

# Install system dependencies
print_status "Installing system dependencies..."
apt-get install -y \
    python3 \
    python3-pip \
    python3-dev \
    python3-venv \
    git \
    build-essential \
    libffi-dev \
    libssl-dev \
    python3-setuptools \
    python3-wheel \
    python3-smbus \
    i2c-tools \
    pigpio \
    python3-pigpio \
    nginx \
    supervisor \
    curl \
    wget \
    nano

# Enable SPI and I2C interfaces
print_status "Enabling SPI interface..."
if ! grep -q "^dtparam=spi=on" /boot/config.txt; then
    echo "dtparam=spi=on" >> /boot/config.txt
    print_info "SPI enabled in /boot/config.txt"
else
    print_info "SPI already enabled"
fi

print_status "Enabling I2C interface..."
if ! grep -q "^dtparam=i2c_arm=on" /boot/config.txt; then
    echo "dtparam=i2c_arm=on" >> /boot/config.txt
    print_info "I2C enabled in /boot/config.txt"
else
    print_info "I2C already enabled"
fi

# Load SPI kernel modules
print_status "Loading SPI kernel modules..."
modprobe spi_bcm2835 2>/dev/null || true
modprobe spidev 2>/dev/null || true

# Add modules to load at boot
if ! grep -q "spi_bcm2835" /etc/modules; then
    echo "spi_bcm2835" >> /etc/modules
fi
if ! grep -q "spidev" /etc/modules; then
    echo "spidev" >> /etc/modules
fi

# Install pigpio daemon
print_status "Setting up pigpio daemon..."
systemctl enable pigpiod
systemctl start pigpiod || true

# Create installation directory
print_status "Creating installation directory..."
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

# Install Python packages globally (as root, no venv)
print_status "Installing Python packages..."

# Upgrade pip first
pip3 install --upgrade pip

# Install specific versions from requirements.txt
pip3 install Flask==2.3.3
pip3 install RPi.GPIO==0.7.1
pip3 install spidev==3.6
pip3 install requests==2.31.0
pip3 install Werkzeug==2.3.7
pip3 install Jinja2==3.1.2
pip3 install MarkupSafe==2.1.3
pip3 install itsdangerous==2.1.2
pip3 install click==8.1.7

# Install gpiozero and pigpio Python library
pip3 install gpiozero==1.6.2
pip3 install pigpio==1.78

# Create project structure
print_status "Creating project structure..."
mkdir -p templates
mkdir -p static
mkdir -p logs

# Copy project files
print_status "Setting up project files..."

# Create a default config.ini if it doesn't exist
if [ ! -f "config.ini" ]; then
    cat > config.ini << 'EOF'
[SYSTEM]
debug = false

[SENSOR]
indoor = true
auto_start = true
sensitivity = high
spi_bus = 0
spi_device = 0
irq_pin = 17
spi_mode = 1
spi_max_hz = 2000000
lco_display_enabled = false
irq_active_high = false

[ALERTS]
energy_threshold = 150000
critical_distance = 16
warning_distance = 32
all_clear_timer = 15

[NOISE_HANDLING]
enabled = true
event_threshold = 15
time_window_seconds = 120
revert_delay_minutes = 10
raised_noise_floor_level = 5

[SLACK]
enabled = false
bot_token = YOUR_SLACK_BOT_TOKEN_HERE
channel = #lightning-alerts

[LOGGING]
level = INFO
max_file_size = 10
backup_count = 5

[DISPLAY]
use_imperial_units = true

[NOISE]
handling_enabled = true
handling_event_threshold = 15
handling_time_window_seconds = 120
handling_raised_noise_floor_level = 5
handling_revert_delay_minutes = 10
EOF
    print_status "Created default config.ini"
else
    print_info "config.ini already exists, keeping existing configuration"
fi

# Create systemd service file
print_status "Creating systemd service..."
cat > /etc/systemd/system/lightning-detector.service << EOF
[Unit]
Description=Lightning Detector Service
After=network.target pigpiod.service
Wants=pigpiod.service

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
Environment="PATH=/usr/bin:/usr/local/bin"
ExecStart=/usr/bin/python3 $INSTALL_DIR/lightning.py
Restart=always
RestartSec=10
StandardOutput=append:$INSTALL_DIR/logs/lightning.log
StandardError=append:$INSTALL_DIR/logs/lightning-error.log

[Install]
WantedBy=multi-user.target
EOF

# Create nginx configuration
print_status "Configuring nginx..."
cat > /etc/nginx/sites-available/lightning-detector << EOF
server {
    listen 80;
    server_name _;
    
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
    }
    
    location /static {
        alias $INSTALL_DIR/static;
    }
}
EOF

# Enable nginx site
ln -sf /etc/nginx/sites-available/lightning-detector /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true

# Test nginx configuration
nginx -t

# Create log rotation configuration
print_status "Setting up log rotation..."
cat > /etc/logrotate.d/lightning-detector << EOF
$INSTALL_DIR/logs/*.log {
    daily
    missingok
    rotate 14
    compress
    delaycompress
    notifempty
    create 0640 root root
    sharedscripts
    postrotate
        systemctl reload lightning-detector 2>/dev/null || true
    endscript
}
EOF

# Create startup script
print_status "Creating startup helper script..."
cat > $INSTALL_DIR/start.sh << 'EOF'
#!/bin/bash
# Lightning Detector Startup Script

echo "Starting Lightning Detector..."

# Ensure pigpiod is running
if ! pgrep -x "pigpiod" > /dev/null; then
    echo "Starting pigpiod..."
    sudo pigpiod
    sleep 2
fi

# Start the application
cd /opt/lightning-detector
python3 lightning.py
EOF
chmod +x $INSTALL_DIR/start.sh

# Create stop script
cat > $INSTALL_DIR/stop.sh << 'EOF'
#!/bin/bash
# Lightning Detector Stop Script

echo "Stopping Lightning Detector..."
sudo systemctl stop lightning-detector
echo "Service stopped."
EOF
chmod +x $INSTALL_DIR/stop.sh

# Create status script
cat > $INSTALL_DIR/status.sh << 'EOF'
#!/bin/bash
# Lightning Detector Status Script

echo "=== Lightning Detector Status ==="
echo ""
echo "Service Status:"
systemctl status lightning-detector --no-pager

echo ""
echo "Pigpiod Status:"
systemctl status pigpiod --no-pager | head -n 5

echo ""
echo "Recent Logs:"
tail -n 10 /opt/lightning-detector/logs/lightning.log 2>/dev/null || echo "No logs yet"

echo ""
echo "SPI Devices:"
ls -la /dev/spi*

echo ""
echo "Web Interface:"
echo "http://$(hostname -I | cut -d' ' -f1)"
EOF
chmod +x $INSTALL_DIR/status.sh

# Set proper permissions
print_status "Setting permissions..."
chown -R root:root $INSTALL_DIR
chmod 755 $INSTALL_DIR
chmod 644 $INSTALL_DIR/*.py 2>/dev/null || true
chmod 644 $INSTALL_DIR/config.ini
chmod 755 $INSTALL_DIR/logs

# Create helper command
print_status "Creating helper command..."
cat > /usr/local/bin/lightning-detector << EOF
#!/bin/bash

case "\$1" in
    start)
        systemctl start lightning-detector
        echo "Lightning Detector started"
        ;;
    stop)
        systemctl stop lightning-detector
        echo "Lightning Detector stopped"
        ;;
    restart)
        systemctl restart lightning-detector
        echo "Lightning Detector restarted"
        ;;
    status)
        $INSTALL_DIR/status.sh
        ;;
    logs)
        journalctl -u lightning-detector -f
        ;;
    config)
        nano $INSTALL_DIR/config.ini
        ;;
    *)
        echo "Usage: lightning-detector {start|stop|restart|status|logs|config}"
        exit 1
        ;;
esac
EOF
chmod +x /usr/local/bin/lightning-detector

# Reload systemd
print_status "Reloading systemd..."
systemctl daemon-reload

# Enable service
print_status "Enabling Lightning Detector service..."
systemctl enable lightning-detector

# Restart nginx
print_status "Restarting nginx..."
systemctl restart nginx

# Test SPI
print_status "Testing SPI interface..."
if [ -e /dev/spidev0.0 ]; then
    print_info "SPI device /dev/spidev0.0 found"
else
    print_warning "SPI device /dev/spidev0.0 not found - reboot may be required"
fi

# Final instructions
echo ""
echo "=================================================="
echo -e "${GREEN}Installation Complete!${NC}"
echo "=================================================="
echo ""
echo -e "${BLUE}Important:${NC}"
echo "1. Copy your project files to: $INSTALL_DIR"
echo "   - lightning.py (main application)"
echo "   - templates/base.html"
echo "   - templates/index.html"
echo "   - templates/config.html"
echo ""
echo "2. Edit configuration: lightning-detector config"
echo "   - Set your Slack bot token if using Slack"
echo "   - Adjust sensor settings as needed"
echo ""
echo "3. Hardware connections (AS3935 to Raspberry Pi):"
echo "   - VCC    → 3.3V (Pin 1 or 17)"
echo "   - GND    → Ground (Pin 6, 9, 14, 20, 25, 30, 34, or 39)"
echo "   - SCL    → SPI0 SCLK (Pin 23, GPIO 11)"
echo "   - MOSI   → SPI0 MOSI (Pin 19, GPIO 10)"
echo "   - MISO   → SPI0 MISO (Pin 21, GPIO 9)"
echo "   - CS     → SPI0 CE0 (Pin 24, GPIO 8)"
echo "   - IRQ    → GPIO 17 (Pin 11) - configurable in config.ini"
echo ""
echo -e "${YELLOW}Commands:${NC}"
echo "  lightning-detector start    - Start the service"
echo "  lightning-detector stop     - Stop the service"
echo "  lightning-detector status   - Check status"
echo "  lightning-detector logs     - View live logs"
echo "  lightning-detector config   - Edit configuration"
echo ""
echo -e "${RED}IMPORTANT:${NC} A reboot is recommended to ensure all"
echo "kernel modules and services are properly loaded."
echo ""
read -p "Would you like to reboot now? (y/n): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    print_info "Rebooting in 5 seconds..."
    sleep 5
    reboot
else
    print_warning "Please reboot manually when convenient"
    print_info "After reboot, start with: lightning-detector start"
fi
