# Lightning Detector

Flask-based web application for monitoring lightning activity using the AS3935 sensor chip (CJMCU-3935 board). Implements event-driven interrupt-based detection with web interface, Slack notifications, and comprehensive diagnostics.

**Version:** 2.1-Production-Enhanced-gpiozero-Imperial-FIXED  
**GPIO Backend:** gpiozero with pigpio support  
**Hardware:** AS3935 lightning detector IC via SPI interface  

## Features

- **Event-driven architecture** - Interrupt-based detection, no polling required
- **Web dashboard** - Real-time monitoring interface with auto-refresh
- **Slack integration** - Automated alerts for warning and critical zones
- **Dynamic noise handling** - Automatic sensitivity adjustment for environmental conditions
- **Imperial/metric units** - Configurable distance display (miles or kilometers)
- **Health monitoring** - Automatic recovery from sensor failures
- **Prometheus metrics** - Compatible monitoring endpoints
- **Comprehensive diagnostics** - Multiple test modes and sensor validation tools

## Hardware Requirements

- Raspberry Pi (any model with GPIO headers)
- CJMCU-3935 lightning detector board (AS3935 chip)
- Jumper wires for connections
- Internet connection (optional, for Slack notifications)

## Hardware Connections

Connect the CJMCU-3935 board to the Raspberry Pi as follows:

| AS3935 Pin | Raspberry Pi Pin | GPIO Number | Description |
|------------|------------------|-------------|-------------|
| VCC | Pin 1 or 17 | - | 3.3V Power |
| GND | Pin 6, 9, 14, 20, 25, 30, 34, or 39 | - | Ground |
| SCL | Pin 23 | GPIO 11 | SPI Clock (SCLK) |
| MOSI | Pin 19 | GPIO 10 | SPI Master Out |
| MISO | Pin 21 | GPIO 9 | SPI Master In |
| CS | Pin 24 | GPIO 8 | SPI Chip Select (CE0) |
| IRQ | Pin 11 | GPIO 17 | Interrupt (configurable) |

## Installation

### Prerequisites

- Raspberry Pi OS (Debian Bullseye or Bookworm)
- Root/sudo access
- Git installed (`sudo apt-get install git`)

### Quick Install

1. Clone the repository:
```bash
git clone https://github.com/CastleLabs/lightning-detector.git
cd lightning-detector
```

2. Run the installation script as root:
```bash
sudo bash install.sh
```

3. Follow the prompts. The script will:
   - Update system packages
   - Install all dependencies (Python, pigpio, nginx, etc.)
   - Enable SPI and I2C interfaces
   - Configure systemd service for automatic startup
   - Set up nginx reverse proxy
   - Create helper commands for management

4. After installation completes, the script will prompt for a reboot. This is recommended to ensure all kernel modules load properly.

### Post-Installation Setup

1. After reboot, edit the configuration file:
```bash
sudo lightning-detector config
```

2. Key settings to configure:
   - **Slack notifications** (if desired):
     - Set `enabled = true` in `[SLACK]` section
     - Add your Slack bot token to `bot_token`
     - Set target `channel` (e.g., `#lightning-alerts`)
   
   - **Alert distances** in `[ALERTS]` section:
     - `critical_distance` - Close strikes requiring immediate action (default: 16 km)
     - `warning_distance` - Distant strikes for awareness (default: 32 km)
   
   - **Sensor settings** in `[SENSOR]` section:
     - `indoor` - Set to `true` for indoor use, `false` for outdoor
     - `sensitivity` - Choose `low`, `medium`, or `high`

3. Start the service:
```bash
sudo lightning-detector start
```

4. Access the web interface:
   - Open a browser and navigate to `http://[raspberry-pi-ip]`
   - The dashboard will show system status and detected events

## System Service Configuration

### Automatic Startup

The installation script creates a systemd service that automatically starts on boot. To verify and manage the service:

```bash
# Enable automatic startup on boot
sudo systemctl enable lightning-detector

# Check if service is enabled
sudo systemctl is-enabled lightning-detector

# Disable automatic startup
sudo systemctl disable lightning-detector
```

### Service Management

The service is configured with automatic restart on failure. The systemd configuration includes:
- **Restart=always** - Automatically restart if the service crashes
- **RestartSec=10** - Wait 10 seconds before restarting
- **After=network.target pigpiod.service** - Ensures network and GPIO daemon are ready

Manual service control:

```bash
# Start the service
sudo systemctl start lightning-detector

# Stop the service
sudo systemctl stop lightning-detector

# Restart the service
sudo systemctl restart lightning-detector

# Check service status
sudo systemctl status lightning-detector

# View service logs
sudo journalctl -u lightning-detector -f
```

### Service Health Monitoring

The system includes multiple layers of failure recovery:

1. **Systemd automatic restart** - Service level recovery
2. **Watchdog thread** - Application level monitoring
3. **Sensor health checks** - Hardware communication verification

To check all components:

```bash
# Full system status
sudo lightning-detector status

# Check if service will start on boot
sudo systemctl list-unit-files | grep lightning-detector

# Verify pigpiod GPIO daemon
sudo systemctl status pigpiod
```

### Manual Service Configuration

If you need to modify the service behavior, edit the systemd unit file:

```bash
# Edit service configuration
sudo nano /etc/systemd/system/lightning-detector.service

# Reload systemd after changes
sudo systemctl daemon-reload

# Restart service with new configuration
sudo systemctl restart lightning-detector
```

Default service configuration (`/etc/systemd/system/lightning-detector.service`):

```ini
[Unit]
Description=Lightning Detector Service
After=network.target pigpiod.service
Wants=pigpiod.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/lightning-detector
Environment="PATH=/usr/bin:/usr/local/bin"
ExecStart=/usr/bin/python3 /opt/lightning-detector/lightning.py
Restart=always
RestartSec=10
StandardOutput=append:/opt/lightning-detector/logs/lightning.log
StandardError=append:/opt/lightning-detector/logs/lightning-error.log

[Install]
WantedBy=multi-user.target
```

## Usage

### Command Line Management

The installation creates a `lightning-detector` helper command for easy control:

```bash
# Quick service control
sudo lightning-detector start    # Start the service
sudo lightning-detector stop     # Stop the service
sudo lightning-detector restart  # Restart the service

# Monitoring
sudo lightning-detector status   # Check service status
sudo lightning-detector logs     # View live logs

# Configuration
sudo lightning-detector config   # Edit configuration file
```

### Web Interface

The web interface provides:

- **Dashboard** (`/`) - System status, alert zones, event history
- **Configuration** (`/config`) - Web-based configuration editor
- **API Status** (`/api/status`) - JSON status endpoint
- **Health Check** (`/health`) - Service health monitoring
- **Metrics** (`/metrics`) - Prometheus-compatible metrics

### Diagnostic Tools

Access diagnostic endpoints for troubleshooting:

- `/full_diagnostic` - Complete sensor and GPIO diagnostic
- `/check_sensor` - Verify and fix sensor configuration
- `/force_trigger` - Generate test interrupt
- `/test_piezo` - Test mode for piezo lighter detection
- `/monitor_interrupts` - Real-time interrupt monitoring

## Configuration

Configuration is stored in `/opt/lightning-detector/config.ini`. Key sections include:

### Detection Settings

```ini
[SENSOR]
indoor = true              # Indoor/outdoor mode
sensitivity = high         # Detection sensitivity: low/medium/high
irq_pin = 17              # GPIO pin for interrupts (BCM numbering)
```

### Alert Configuration

```ini
[ALERTS]
critical_distance = 16     # Critical zone distance (km)
warning_distance = 32      # Warning zone distance (km)
energy_threshold = 150000  # Minimum energy for alerts
all_clear_timer = 15       # Minutes before all-clear message
```

### Display Units

```ini
[DISPLAY]
use_imperial_units = true  # true for miles, false for kilometers
```

### Slack Integration

```ini
[SLACK]
enabled = false
bot_token = xoxb-your-token-here
channel = #lightning-alerts
```

## Alert System

### Alert Zones

- **Critical Zone** - Immediate danger, take shelter (default: ≤16 km / ≤10 miles)
- **Warning Zone** - Lightning in area, be prepared (default: ≤32 km / ≤20 miles)
- **All Clear** - No activity for configured time period (default: 15 minutes)

### Alert Flow

1. Lightning detected via interrupt signal
2. Distance and energy calculated from sensor
3. If within alert zone and above energy threshold:
   - Send Slack notification (if enabled)
   - Update web dashboard status
   - Start all-clear timer
4. If no strikes detected for timer duration, send all-clear message

## Troubleshooting

### SPI Communication Issues

1. Verify SPI is enabled:
```bash
ls -la /dev/spi*
# Should show /dev/spidev0.0 and /dev/spidev0.1
```

2. Check connections:
   - Ensure all wires are firmly connected
   - Verify 3.3V power (not 5V)
   - Check ground connection

3. Test SPI communication:
```bash
sudo lightning-detector status
# Check for sensor health status
```

### No Lightning Detection

1. Check sensor mode matches environment:
   - Indoor mode for inside buildings
   - Outdoor mode for open areas

2. Adjust sensitivity:
   - Increase for more sensitive detection
   - Decrease if getting too many false positives

3. Use diagnostic tools:
```bash
# Test with piezo lighter
curl http://localhost/test_piezo

# Check sensor registers
curl http://localhost/check_sensor
```

### False Positives

1. Enable noise handling (enabled by default):
   - System automatically adjusts sensitivity
   - Monitors disturber events and adapts

2. Check for interference sources:
   - Fluorescent lights
   - Motors and fans
   - Other electronic devices

3. Adjust configuration:
   - Increase `energy_threshold` value
   - Set sensitivity to `medium` or `low`
   - Use outdoor mode for noisy environments

### Service Issues

1. Check service status:
```bash
sudo systemctl status lightning-detector
sudo systemctl status pigpiod
```

2. View logs for errors:
```bash
sudo journalctl -u lightning-detector -n 50
tail -f /opt/lightning-detector/logs/lightning.log
```

3. Restart services:
```bash
sudo systemctl restart pigpiod
sudo systemctl restart lightning-detector
```

## System Architecture

### Threading Model
- **Main Thread** - Flask web server
- **Monitoring Thread** - Sensor initialization and health monitoring
- **Watchdog Thread** - Monitors and restarts monitoring thread if needed
- **Slack Worker Thread** - Non-blocking notification queue processor
- **Timer Threads** - All-clear message scheduling

### Data Storage
- **Events** - In-memory circular buffer (100 events maximum)
- **Configuration** - File-based with runtime caching
- **State** - Thread-safe dictionaries with locks
- **Persistence** - No database; data resets on restart

### Error Recovery
- Automatic sensor reinitialization on failure
- Periodic health checks every 5 minutes
- Watchdog thread restarts monitoring if crashed
- Maximum 3 consecutive failures before stopping

## API Endpoints

### Status and Monitoring

- `GET /api/status` - JSON system status
- `GET /health` - Health check (returns 200 or 503)
- `GET /metrics` - Prometheus-compatible metrics

### System Control

- `GET /start_monitoring` - Start detection system
- `GET /stop_monitoring` - Stop detection system
- `GET /reset_alerts` - Clear all active alerts

### Testing (Debug Mode)

- `GET /test_alerts?type={warning|critical}` - Test alert system
- `GET /test_slack` - Test Slack integration

## Slack Setup

1. Create a Slack app:
   - Visit [api.slack.com/apps](https://api.slack.com/apps)
   - Click "Create New App" → "From scratch"
   - Name your app and select workspace

2. Configure OAuth permissions:
   - Navigate to "OAuth & Permissions"
   - Add scope: `chat:write`
   - Install app to workspace
   - Copy "Bot User OAuth Token" (starts with `xoxb-`)

3. Add bot to channel:
   - In Slack, invite bot to target channel: `/invite @your-bot-name`

4. Update configuration:
   - Set token in config: `sudo lightning-detector config`
   - Enable Slack notifications
   - Set target channel

## Performance Specifications

- **Detection Range** - 1-40 km (hardware limited)
- **Energy Resolution** - 20-bit value (0-1,048,575)
- **Interrupt Response** - <10ms typical
- **Web Refresh Rate** - 30 seconds (configurable)
- **Event Buffer** - 100 events maximum
- **Log Rotation** - 10MB per file, 5 files retained
- **Health Check Interval** - 5 minutes

## File Locations

- **Application** - `/opt/lightning-detector/`
- **Configuration** - `/opt/lightning-detector/config.ini`
- **Logs** - `/opt/lightning-detector/logs/`
- **Service** - `/etc/systemd/system/lightning-detector.service`
- **Nginx Config** - `/etc/nginx/sites-available/lightning-detector`
- **Helper Script** - `/usr/local/bin/lightning-detector`
