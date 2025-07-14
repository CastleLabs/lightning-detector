#!/bin/bash
# Lightning Detector Enhanced Setup Script v1.12
# Automated installation and configuration for Raspberry Pi
# Enhanced Alert System with Warning/Critical/All-Clear functionality

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_enhanced() {
    echo -e "${PURPLE}[ENHANCED]${NC} $1"
}

# Enhanced Header
echo -e "${CYAN}"
echo "🌩️  Lightning Detector Enhanced Setup v1.12"
echo "=============================================="
echo "Enhanced Alert System with Warning/Critical/All-Clear"
echo -e "${NC}"

# Check if running as root
if [ "$EUID" -eq 0 ]; then
    print_error "Please do not run this script as root. Run as a regular user."
    exit 1
fi

# Check if running on Raspberry Pi
print_status "Checking system compatibility..."
if ! grep -q "Raspberry Pi" /proc/cpuinfo 2>/dev/null; then
    print_warning "This doesn't appear to be a Raspberry Pi"
    print_warning "Hardware-specific features may not work"
    read -p "Continue anyway? (y/n): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
else
    print_success "Raspberry Pi detected"
fi

# Enhanced system update
print_status "Updating system packages..."
sudo apt update && sudo apt upgrade -y

# Install enhanced system dependencies
print_status "Installing enhanced system dependencies..."
sudo apt install -y python3 python3-pip python3-venv git curl wget

# Check Python version
PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
print_status "Python version: $PYTHON_VERSION"

if [[ $(echo "$PYTHON_VERSION 3.7" | awk '{print ($1 >= $2)}') == 1 ]]; then
    print_success "Python version is compatible with enhanced features"
else
    print_warning "Python version may not support all enhanced features"
fi

# Enhanced SPI interface setup
print_status "Enabling SPI interface for enhanced sensor communication..."
if sudo raspi-config nonint do_spi 0; then
    print_success "SPI interface enabled for enhanced monitoring"
else
    print_warning "Failed to enable SPI - you may need to enable it manually"
fi

# Enhanced GPIO permissions
print_status "Setting up enhanced GPIO permissions..."
sudo usermod -a -G gpio,spi "$USER"
print_warning "You may need to REBOOT or RE-LOGIN for GPIO/SPI permissions to apply."

# Create enhanced project directory structure
print_status "Creating enhanced project structure..."
mkdir -p templates
mkdir -p logs
mkdir -p backups

# Create enhanced virtual environment
print_enhanced "Creating enhanced Python virtual environment..."
python3 -m venv lightning_detector_env
source lightning_detector_env/bin/activate

# Upgrade pip
print_status "Upgrading pip for enhanced package management..."
pip install --upgrade pip

# Install enhanced Python packages
print_enhanced "Installing enhanced Python packages with alert system dependencies..."
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
    print_success "Enhanced Python packages installed from requirements.txt"
else
    print_warning "requirements.txt not found, installing packages manually..."
    pip install Flask==2.3.3 RPi.GPIO==0.7.1 spidev==3.6 requests==2.31.0
fi

# Move HTML templates to proper directory
print_status "Moving enhanced HTML templates to templates directory..."
if ls *.html &> /dev/null; then
    mv *.html templates/
    print_success "Enhanced templates moved to templates/ directory"
else
    print_status "No HTML files found in the root directory to move."
fi


# Create enhanced config.ini with new alert system settings
if [ ! -f "config.ini" ]; then
    print_enhanced "Creating enhanced config.ini with v1.12 alert system..."
    cat > config.ini << EOF
[SENSOR]
spi_bus = 0
spi_device = 0
irq_pin = 2
polling_interval = 1.0
sensitivity = medium
auto_start = true

[SLACK]
bot_token =
channel = #alerts
enabled = true

[ALERTS]
# Enhanced Alert System Configuration v1.12
# WARNING ALERTS: Lightning within 30km (but > 10km)
# CRITICAL ALERTS: Lightning within 10km
# ALL-CLEAR: Sent after 15 minutes of no lightning in respective zones

# Alert zones (distances in kilometers)
warning_distance = 30
critical_distance = 10

# Timer settings (in minutes)
all_clear_timer = 15

# Minimum energy level to trigger any alert
energy_threshold = 100000

# Legacy settings (kept for compatibility)
min_distance = 1
max_distance = 40
cooldown_minutes = 5

[LOGGING]
level = INFO
max_file_size = 10
backup_count = 5
EOF
    print_success "Enhanced config.ini v1.12 created with alert system"
else
    print_status "Existing config.ini found, backing up..."
    cp config.ini "backups/config.ini.backup.$(date +%Y%m%d_%H%M%S)"
    print_success "Config backup created"
fi

# Enhanced systemd service setup
SERVICE_REPLY=""
echo
read -p "🚀 Set up enhanced system service with v1.12 features (auto-start on boot)? (y/n): " -n 1 -r SERVICE_REPLY
echo
if [[ $SERVICE_REPLY =~ ^[Yy]$ ]]; then
    print_enhanced "Creating enhanced systemd service..."

    # Create enhanced service file
    sudo tee /etc/systemd/system/lightning-detector.service > /dev/null <<EOF
[Unit]
Description=Lightning Detector Enhanced Service v1.12
After=network.target
Wants=network.target

[Service]
Type=simple
User=$USER
Group=$USER
WorkingDirectory=$(pwd)
Environment=PATH=$(pwd)/lightning_detector_env/bin
ExecStart=$(pwd)/lightning_detector_env/bin/python lightning.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# Enhanced service settings
TimeoutStartSec=30
TimeoutStopSec=15
KillMode=mixed

[Install]
WantedBy=multi-user.target
EOF

    # Reload systemd and enable service
    sudo systemctl daemon-reload
    sudo systemctl enable lightning-detector.service

    print_success "Enhanced service v1.12 created and enabled!"
    print_enhanced "Enhanced service commands:"
    echo "  Start:   sudo systemctl start lightning-detector"
    echo "  Stop:    sudo systemctl stop lightning-detector"
    echo "  Status:  sudo systemctl status lightning-detector"
    echo "  Logs:    sudo journalctl -u lightning-detector -f"
fi

# Create enhanced startup script
print_enhanced "Creating enhanced startup script with v1.12 features..."
cat > start.sh << 'EOF'
#!/bin/bash
# Lightning Detector Enhanced Startup Script v1.12
echo "Starting Lightning Detector Enhanced v1.12..."
echo "Enhanced Alert System: Warning/Critical/All-Clear"
cd "$(dirname "$0")"
source lightning_detector_env/bin/activate
python lightning.py
EOF

chmod +x start.sh
print_success "Enhanced startup script created (start.sh)"

# Enhanced file permissions
print_status "Setting enhanced file permissions..."
chmod 644 config.ini
chmod 755 lightning.py
chmod 755 setup.sh
chmod 755 start.sh

# Create enhanced maintenance scripts
print_enhanced "Creating enhanced maintenance scripts..."

# Log viewer script
cat > view_logs.sh << 'EOF'
#!/bin/bash
# Enhanced Log Viewer v1.12
echo "Lightning Detector Enhanced Logs v1.12"
echo "======================================"
if [ -f "lightning_detector.log" ]; then
    tail -f lightning_detector.log
else
    echo "No log file found. Start the application first."
fi
EOF
chmod +x view_logs.sh

# Status checker script
cat > check_status.sh << 'EOF'
#!/bin/bash
# Enhanced Status Checker v1.12
echo "Lightning Detector Enhanced Status v1.12"
echo "========================================"
echo "Service Status:"
sudo systemctl status lightning-detector --no-pager -l
echo ""
echo "Recent Logs:"
sudo journalctl -u lightning-detector --no-pager -l -n 20
EOF
chmod +x check_status.sh

print_success "Enhanced maintenance scripts created"

# Get IP address for enhanced web interface
# This method is generally reliable but might pick the wrong IP on complex network setups
IP_ADDRESS=$(hostname -I | awk '{print $1}')

# Enhanced completion summary
echo
print_success "Enhanced Lightning Detector v1.12 setup complete!"
echo
echo "📋 Enhanced Next Steps:"
echo "┌─────────────────────────────────────────────────────────────────┐"
echo "│ ⚠️  IMPORTANT: Please REBOOT or RE-LOGIN now to apply all changes, │"
echo "│    especially for hardware interface permissions.               │"
echo "│                                                                 │"
echo "│ 🔧 Hardware Setup:                                              │"
echo "│    Connect CJMCU-3935 to Raspberry Pi SPI pins                 │"
echo "│    VCC → 3.3V, GND → GND, MOSI → GPIO10, MISO → GPIO9         │"
echo "│    SCLK → GPIO11, CS → GPIO8, IRQ → GPIO2                      │"
echo "│                                                                 │"
echo "│ ⚙️  Enhanced Configuration v1.12:                               │"
echo "│    Edit config.ini to add your Slack bot token                 │"
echo "│    Configure warning/critical distances and other settings     │"
echo "│                                                                 │"
echo "│ 🚀 Enhanced Running Options (after reboot/re-login):            │"
echo "│    Manual start: ./start.sh                                    │"
if [[ $SERVICE_REPLY =~ ^[Yy]$ ]]; then
echo "│    Service start: sudo systemctl start lightning-detector      │"
fi
echo "│    View logs: ./view_logs.sh                                   │"
echo "│    Check status: ./check_status.sh                            │"
echo "│                                                                 │"
echo "│ 🌐 Enhanced Web Interface v1.12:                               │"
echo "│    Dashboard: http://$IP_ADDRESS:5000                   │"
echo "│    Configuration: http://$IP_ADDRESS:5000/config       │"
echo "│    API Status: http://$IP_ADDRESS:5000/api/status      │"
echo "└─────────────────────────────────────────────────────────────────┘"
echo
print_enhanced "Enhanced Features v1.12:"
echo "🔹 Warning/Critical/All-Clear alert system"
echo "🔹 Smart timer management (configurable time)"
echo "🔹 Rich Slack message formatting"
echo "🔹 Enhanced web dashboard with alert status"
echo
print_warning "Note: You must reboot or re-login for hardware changes to take effect."
echo

# Enhanced immediate start option
START_REPLY=""
read -p "🏃 Start the enhanced Lightning Detector v1.12 now? (y/n): " -n 1 -r START_REPLY
echo
if [[ $START_REPLY =~ ^[Yy]$ ]]; then
    print_enhanced "Starting Enhanced Lightning Detector v1.12..."
    echo "Enhanced Alert System Features:"
    echo "• WARNING alerts: Lightning ≤30km"
    echo "• CRITICAL alerts: Lightning ≤10km"
    echo "• ALL-CLEAR: After 15 minutes no lightning"
    echo ""
    echo "Press Ctrl+C to stop"
    echo "Enhanced web interface: http://$IP_ADDRESS:5000"
    echo
    ./start.sh
fi

print_success "Enhanced Lightning Detector v1.12 setup completed successfully!"
print_enhanced "Welcome to the most advanced lightning detection system!"
