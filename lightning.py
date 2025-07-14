#!/usr/bin/env python3
"""
CJMCU-3935 Lightning Detector Flask Application v1.12
Enhanced Alert System with Warning/Critical/All-Clear functionality
(Final patches for stability, robustness, and consistency)
"""

import configparser
import os
import threading
import time
import json
import logging
import atexit
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from collections import deque
from enum import Enum

import requests
import RPi.GPIO as GPIO
import spidev
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash

# --- Alert Level Enumeration ---
class AlertLevel(Enum):
    WARNING = "warning"
    CRITICAL = "critical"
    ALL_CLEAR = "all_clear"

# --- Global Configuration and State ---
CONFIG = configparser.ConfigParser()

# Main monitoring state - thread-safe dictionary to hold all shared state
MONITORING_STATE = {
    "lock": threading.Lock(),
    "stop_event": threading.Event(),
    "events": deque(maxlen=100),
    "status": {'last_reading': None, 'sensor_active': False, 'status_message': 'Not started'},
    "thread": None,
}

# Enhanced alert state management
ALERT_STATE = {
    "warning_timer": None,
    "critical_timer": None,
    "warning_active": False,
    "critical_active": False,
    "last_warning_strike": None,
    "last_critical_strike": None,
    "timer_lock": threading.Lock()
}

# Add a dedicated lock for sensor initialization to ensure it is atomic.
SENSOR_INIT_LOCK = threading.Lock()
sensor = None # Global sensor object

# --- Flask App Initialization ---
app = Flask(__name__)
app.secret_key = 'lightning-detector-enhanced-v112-secret-key'

# --- Sensor Driver Class ---
class AS3935LightningDetector:
    """Driver for CJMCU-3935 (AS3935) Lightning Detector"""
    # Register addresses
    REG_AFE_GAIN = 0x00
    REG_PWD = 0x00
    REG_NF_LEV = 0x01
    REG_WDTH = 0x01
    REG_CL_STAT = 0x02
    REG_MIN_NUM_LIGH = 0x02
    REG_SREJ = 0x02
    REG_LCO_FDIV = 0x03
    REG_MASK_DIST = 0x03
    REG_DISP_LCO = 0x08
    REG_DISP_SRCO = 0x08
    REG_DISP_TRCO = 0x08
    REG_TUN_CAP = 0x08

    # Interrupt reasons
    INT_NH = 0x01  # Noise level too high
    INT_D = 0x04   # Disturber detected
    INT_L = 0x08   # Lightning detected

    SENSITIVITY_SETTINGS = {
        'low': {'afe_gain': 0x24, 'noise_floor': 0x02, 'watchdog_threshold': 0x03, 'spike_rejection': 0x03},
        'medium': {'afe_gain': 0x24, 'noise_floor': 0x02, 'watchdog_threshold': 0x02, 'spike_rejection': 0x02},
        'high': {'afe_gain': 0x24, 'noise_floor': 0x01, 'watchdog_threshold': 0x01, 'spike_rejection': 0x01}
    }

    def __init__(self, spi_bus=0, spi_device=0, irq_pin=2):
        self.spi = None
        self.irq_pin = irq_pin
        self.is_initialized = False

        try:
            self.spi = spidev.SpiDev()
            self.spi.open(spi_bus, spi_device)
            self.spi.max_speed_hz = 2000000
            self.spi.mode = 0b01  # CPOL=0, CPHA=1

            GPIO.setwarnings(False) # Disable warnings for channel usage
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.irq_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

            self.power_up()
            self.is_initialized = True
        except Exception as e:
            self.cleanup() # Ensure cleanup is called on init failure
            raise e

    def _write_register(self, reg, value):
        if self.spi:
            self.spi.xfer2([reg, value])

    def _read_register(self, reg):
        if self.spi:
            result = self.spi.xfer2([reg | 0x40, 0x00])
            return result[1]
        return 0

    def power_up(self):
        """Initializes and calibrates the sensor."""
        self._write_register(self.REG_PWD, 0x96)
        time.sleep(0.003)
        self._write_register(self.REG_LCO_FDIV, 0b10010111)
        self._write_register(self.REG_DISP_LCO, 0x80)
        time.sleep(0.002)
        self._write_register(self.REG_DISP_LCO, 0x00)
        time.sleep(0.002)
        sensitivity = CONFIG.get('SENSOR', 'sensitivity', fallback='medium')
        settings = self.SENSITIVITY_SETTINGS.get(sensitivity, self.SENSITIVITY_SETTINGS['medium'])
        self._write_register(self.REG_AFE_GAIN, settings['afe_gain'])
        self._write_register(self.REG_NF_LEV, settings['noise_floor'])
        self._write_register(self.REG_WDTH, settings['watchdog_threshold'])
        self._write_register(self.REG_SREJ, settings['spike_rejection'])
        app.logger.info(f"Sensor powered up with {sensitivity} sensitivity.")

    def get_interrupt_reason(self):
        return self._read_register(0x03) & 0x0F

    def get_lightning_distance(self):
        return self._read_register(0x07) & 0x3F

    def get_lightning_energy(self):
        lsb = self._read_register(0x04)
        msb = self._read_register(0x05)
        mmsb = self._read_register(0x06) & 0x1F
        return (mmsb << 16) | (msb << 8) | lsb

    def is_interrupt_detected(self):
        return GPIO.input(self.irq_pin) == GPIO.LOW

    def cleanup(self):
        """Safe cleanup only affecting this application's resources."""
        try:
            if self.spi:
                self.spi.close()
                self.spi = None
            if self.is_initialized:
                 GPIO.cleanup(self.irq_pin)
            app.logger.info(f"Cleaned up GPIO pin {self.irq_pin} and SPI resources.")
        except Exception as e:
            app.logger.error(f"Error during hardware cleanup: {e}")

# --- Helper functions for robust config reading ---
def get_config_int(section, key, fallback):
    """Safely get an integer from the config."""
    try:
        return CONFIG.getint(section, key)
    except (ValueError, configparser.NoOptionError, configparser.NoSectionError):
        app.logger.warning(f"Invalid or missing value for '{key}' in [{section}]. Using fallback: {fallback}.")
        return fallback

def get_config_float(section, key, fallback):
    """Safely get a float from the config."""
    try:
        return CONFIG.getfloat(section, key)
    except (ValueError, configparser.NoOptionError, configparser.NoSectionError):
        app.logger.warning(f"Invalid or missing value for '{key}' in [{section}]. Using fallback: {fallback}.")
        return fallback

def get_config_boolean(section, key, fallback):
    """Safely get a boolean from the config."""
    try:
        return CONFIG.getboolean(section, key)
    except (ValueError, configparser.NoOptionError, configparser.NoSectionError):
        app.logger.warning(f"Invalid or missing value for '{key}' in [{section}]. Using fallback: {fallback}.")
        return fallback

# --- Enhanced Alert System Functions ---
def schedule_all_clear_message(alert_level):
    """Schedules an all-clear message. Timer cancellation is thread-safe."""
    delay_minutes = get_config_int('ALERTS', 'all_clear_timer', 15)
    def send_all_clear():
        with ALERT_STATE["timer_lock"]:
            now = datetime.now()
            if alert_level == AlertLevel.WARNING and ALERT_STATE["warning_active"]:
                if ALERT_STATE["last_warning_strike"] and (now - ALERT_STATE["last_warning_strike"]) >= timedelta(minutes=delay_minutes):
                    send_slack_notification(f"🟢 All Clear: No lightning detected within {get_config_int('ALERTS', 'warning_distance', 30)}km for {delay_minutes} minutes.", alert_level=AlertLevel.ALL_CLEAR, previous_level=AlertLevel.WARNING)
                    ALERT_STATE["warning_active"] = False
                    ALERT_STATE["warning_timer"] = None
            elif alert_level == AlertLevel.CRITICAL and ALERT_STATE["critical_active"]:
                if ALERT_STATE["last_critical_strike"] and (now - ALERT_STATE["last_critical_strike"]) >= timedelta(minutes=delay_minutes):
                    send_slack_notification(f"🟢 All Clear: No lightning detected within {get_config_int('ALERTS', 'critical_distance', 10)}km for {delay_minutes} minutes.", alert_level=AlertLevel.ALL_CLEAR, previous_level=AlertLevel.CRITICAL)
                    ALERT_STATE["critical_active"] = False
                    ALERT_STATE["critical_timer"] = None
    with ALERT_STATE["timer_lock"]:
        if alert_level == AlertLevel.WARNING and ALERT_STATE["warning_timer"]:
            ALERT_STATE["warning_timer"].cancel()
        elif alert_level == AlertLevel.CRITICAL and ALERT_STATE["critical_timer"]:
            ALERT_STATE["critical_timer"].cancel()
        timer = threading.Timer(delay_minutes * 60, send_all_clear)
        timer.daemon = True
        timer.start()
        if alert_level == AlertLevel.WARNING:
            ALERT_STATE["warning_timer"] = timer
        elif alert_level == AlertLevel.CRITICAL:
            ALERT_STATE["critical_timer"] = timer

def check_alert_conditions(distance, energy):
    with ALERT_STATE["timer_lock"]:
        now = datetime.now()
        should_send_alert = False
        alert_level = None
        energy_threshold = get_config_int('ALERTS', 'energy_threshold', 100000)
        critical_distance = get_config_int('ALERTS', 'critical_distance', 10)
        warning_distance = get_config_int('ALERTS', 'warning_distance', 30)
        if energy < energy_threshold: return {"send_alert": False, "level": None}
        if distance <= critical_distance:
            ALERT_STATE["last_critical_strike"] = now
            if not ALERT_STATE["critical_active"]:
                ALERT_STATE["critical_active"] = True
                should_send_alert = True
                alert_level = AlertLevel.CRITICAL
                ALERT_STATE["warning_active"] = False
            schedule_all_clear_message(AlertLevel.CRITICAL)
        elif distance <= warning_distance and not ALERT_STATE["critical_active"]:
            ALERT_STATE["last_warning_strike"] = now
            if not ALERT_STATE["warning_active"]:
                ALERT_STATE["warning_active"] = True
                should_send_alert = True
                alert_level = AlertLevel.WARNING
            schedule_all_clear_message(AlertLevel.WARNING)
    return {"send_alert": should_send_alert, "level": alert_level}

def send_slack_notification(message, distance=None, energy=None, alert_level=None, previous_level=None):
    if not get_config_boolean('SLACK', 'enabled', False): return
    bot_token = CONFIG.get('SLACK', 'bot_token', fallback='')
    channel = CONFIG.get('SLACK', 'channel', fallback='#alerts')
    if not bot_token:
        app.logger.warning("Slack is enabled, but Bot Token is not configured.")
        return
    url = 'https://slack.com/api/chat.postMessage'
    if alert_level == AlertLevel.CRITICAL: color, emoji, urgency = "#ff0000", ":rotating_light:", "CRITICAL"
    elif alert_level == AlertLevel.WARNING: color, emoji, urgency = "#ff9900", ":warning:", "WARNING"
    elif alert_level == AlertLevel.ALL_CLEAR: color, emoji, urgency = "#00ff00", ":white_check_mark:", "ALL CLEAR"
    else: color, emoji, urgency = "#ffcc00", ":zap:", "INFO"
    blocks = []
    if alert_level in [AlertLevel.WARNING, AlertLevel.CRITICAL]:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"{emoji} *{urgency} LIGHTNING ALERT* {emoji}\n{message}"}})
        if distance is not None and energy is not None: blocks.append({"type": "section", "fields": [{"type": "mrkdwn", "text": f"*Distance:*\n{distance} km"}, {"type": "mrkdwn", "text": f"*Energy Level:*\n{energy:,}"}, {"type": "mrkdwn", "text": f"*Alert Level:*\n{urgency}"}, {"type": "mrkdwn", "text": f"*Time:*\n{datetime.now().strftime('%H:%M:%S')}"}]})
        context_text = ":exclamation: *Very close strike. Take shelter.*" if alert_level == AlertLevel.CRITICAL else ":cloud_with_lightning: *Activity in the area. Be prepared.*"
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": context_text}]})
    elif alert_level == AlertLevel.ALL_CLEAR:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"{emoji} *{urgency}*\n{message}"}})
        previous_urgency = "WARNING" if previous_level == AlertLevel.WARNING else "CRITICAL"
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f":information_source: No strikes in {previous_urgency.lower()} zone for {get_config_int('ALERTS', 'all_clear_timer', 15)} min."}]})
    else: blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"{emoji} {message}"}})
    payload = {'channel': channel, 'text': message, 'blocks': blocks, 'icon_emoji': emoji}
    if alert_level in [AlertLevel.CRITICAL, AlertLevel.WARNING, AlertLevel.ALL_CLEAR]: payload['attachments'] = [{'color': color, 'fallback': message}]
    headers = {'Authorization': f'Bearer {bot_token}', 'Content-Type': 'application/json'}
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        result = response.json()
        if not result.get('ok'): app.logger.error(f"Slack API error: {result.get('error', 'Unknown error')}")
    except requests.exceptions.RequestException as e: app.logger.error(f"Slack notification failed (RequestException): {e}")
    except Exception as e: app.logger.error(f"Unexpected error sending Slack notification: {e}")

def cleanup_alert_timers():
    with ALERT_STATE["timer_lock"]:
        if ALERT_STATE["warning_timer"]: ALERT_STATE["warning_timer"].cancel()
        if ALERT_STATE["critical_timer"]: ALERT_STATE["critical_timer"].cancel()
        ALERT_STATE["warning_timer"] = None
        ALERT_STATE["critical_timer"] = None
        ALERT_STATE["warning_active"] = False
        ALERT_STATE["critical_active"] = False
    app.logger.info("Alert timers cleaned up.")

# --- Monitoring Logic ---
def lightning_monitoring():
    """Main monitoring loop with robust initialization and retry limits."""
    global sensor
    MAX_SENSOR_RETRIES = 5
    retry_count = 0

    while not MONITORING_STATE['stop_event'].is_set():
        with SENSOR_INIT_LOCK:
            if sensor is None:
                if retry_count >= MAX_SENSOR_RETRIES:
                    app.logger.critical(f"Sensor initialization failed after {MAX_SENSOR_RETRIES} attempts. Monitoring thread stopping.")
                    with MONITORING_STATE['lock']:
                         MONITORING_STATE['status']['status_message'] = f"Fatal: Max sensor retries exceeded."
                    break # Exit the while loop

                try:
                    spi_bus = get_config_int('SENSOR', 'spi_bus', 0)
                    spi_device = get_config_int('SENSOR', 'spi_device', 0)
                    irq_pin = get_config_int('SENSOR', 'irq_pin', 2)
                    sensor = AS3935LightningDetector(spi_bus, spi_device, irq_pin)

                    with MONITORING_STATE['lock']:
                        MONITORING_STATE['status']['sensor_active'] = True
                        MONITORING_STATE['status']['status_message'] = "Monitoring active."
                    app.logger.info("Lightning sensor initialized successfully.")
                    retry_count = 0 # Reset counter on success
                except Exception as e:
                    retry_count += 1
                    with MONITORING_STATE['lock']:
                        MONITORING_STATE['status']['sensor_active'] = False
                        MONITORING_STATE['status']['status_message'] = f"Sensor init failed (attempt {retry_count})."
                    app.logger.error(f"Sensor init failed (attempt {retry_count}/{MAX_SENSOR_RETRIES}): {e}. Retrying in 30 seconds.")
                    MONITORING_STATE['stop_event'].wait(30)
                    continue

        try:
            if sensor and sensor.is_interrupt_detected():
                time.sleep(0.005)
                interrupt_reason = sensor.get_interrupt_reason()
                if interrupt_reason == sensor.INT_L:
                    distance = sensor.get_lightning_distance()
                    energy = sensor.get_lightning_energy()
                    alert_result = check_alert_conditions(distance, energy)
                    event = {'timestamp': datetime.now().isoformat(), 'distance': distance, 'energy': energy, 'alert_sent': alert_result.get('send_alert', False), 'alert_level': alert_result.get('level').value if alert_result.get('level') else None}
                    with MONITORING_STATE['lock']: MONITORING_STATE['events'].append(event)
                    app.logger.info(f"⚡ Lightning detected: {distance}km, energy: {energy}")
                    if alert_result.get('send_alert'):
                        level = alert_result.get('level')
                        if level == AlertLevel.CRITICAL: send_slack_notification(f"🚨 CRITICAL: Lightning strike detected! Distance: {distance}km", distance, energy, level)
                        elif level == AlertLevel.WARNING: send_slack_notification(f"⚠️ WARNING: Lightning detected. Distance: {distance}km", distance, energy, level)
                elif interrupt_reason == sensor.INT_D: app.logger.debug("Disturber detected")
                elif interrupt_reason == sensor.INT_NH: app.logger.warning("Noise level too high")

            with MONITORING_STATE['lock']:
                MONITORING_STATE['status']['last_reading'] = datetime.now().isoformat()

            polling_interval = get_config_float('SENSOR', 'polling_interval', 1.0)
            MONITORING_STATE['stop_event'].wait(polling_interval)

        except Exception as e:
            app.logger.error(f"Error in monitoring loop: {e}. Resetting sensor.", exc_info=True)
            with SENSOR_INIT_LOCK:
                if sensor:
                    sensor.cleanup()
                sensor = None # Force re-initialization on next loop
            time.sleep(5)

    with SENSOR_INIT_LOCK:
        if sensor:
            sensor.cleanup()
            sensor = None
    with MONITORING_STATE['lock']:
        MONITORING_STATE['status']['sensor_active'] = False
    app.logger.info("Lightning monitoring thread has gracefully shut down.")

# --- Helper Functions & Routes ---
def test_slack_connection():
    if not get_config_boolean('SLACK', 'enabled', False): return False, "Slack notifications are disabled"
    bot_token = CONFIG.get('SLACK', 'bot_token', fallback='')
    if not bot_token: return False, "Bot token is not configured"
    try:
        response = requests.get('https://slack.com/api/auth.test', headers={'Authorization': f'Bearer {bot_token}'}, timeout=10)
        response.raise_for_status()
        result = response.json()
        if result.get('ok'): return True, f"Connected as {result.get('user', 'Unknown')} to {result.get('team', 'Unknown')}"
        else: return False, f"Authentication failed: {result.get('error', 'Unknown error')}"
    except Exception as e: return False, f"Connection error: {str(e)}"

@app.route('/')
def index():
    with MONITORING_STATE['lock']:
        events = list(MONITORING_STATE['events'])
        status = MONITORING_STATE['status'].copy()
    with ALERT_STATE["timer_lock"]:
        alert_status = {'warning_active': ALERT_STATE["warning_active"], 'critical_active': ALERT_STATE["critical_active"], 'last_warning_strike': ALERT_STATE["last_warning_strike"].strftime('%H:%M:%S') if ALERT_STATE["last_warning_strike"] else None, 'last_critical_strike': ALERT_STATE["last_critical_strike"].strftime('%H:%M:%S') if ALERT_STATE["last_critical_strike"] else None}
    for event in events:
        event['timestamp'] = datetime.fromisoformat(event['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
    if status.get('last_reading'): status['last_reading'] = datetime.fromisoformat(status['last_reading']).strftime('%Y-%m-%d %H:%M:%S')
    return render_template('index.html', lightning_events=events, sensor_status=status, alert_state=alert_status, debug_mode=app.debug)

@app.route('/api/status')
def api_status():
    with MONITORING_STATE['lock']:
        events = list(MONITORING_STATE['events'])
        status = MONITORING_STATE['status'].copy()
        thread_alive = MONITORING_STATE['thread'].is_alive() if MONITORING_STATE.get('thread') else False
    with ALERT_STATE["timer_lock"]:
        alert_status = {'warning_active': ALERT_STATE["warning_active"], 'critical_active': ALERT_STATE["critical_active"], 'last_warning_strike': ALERT_STATE["last_warning_strike"].isoformat() if ALERT_STATE["last_warning_strike"] else None, 'last_critical_strike': ALERT_STATE["last_critical_strike"].isoformat() if ALERT_STATE["last_critical_strike"] else None}
    return jsonify({**status, 'alert_state': alert_status, 'monitoring_thread_active': thread_alive, 'recent_events_count': len(events), 'version': '1.12-patched'})

@app.route('/config')
def config_page():
    return render_template('config.html', config=CONFIG, debug_mode=app.debug)

@app.route('/save_config', methods=['POST'])
def save_config_route():
    try:
        for key, value in request.form.items():
            if '_' in key:
                section, option = key.split('_', 1)
                if section in CONFIG: CONFIG.set(section, option, value)
        CONFIG.set('SLACK', 'enabled', 'true' if 'SLACK_enabled' in request.form else 'false')
        CONFIG.set('SENSOR', 'auto_start', 'true' if 'SENSOR_auto_start' in request.form else 'false')
        with open('config.ini', 'w') as configfile: CONFIG.write(configfile)
        flash('Configuration saved successfully! A restart may be required.', 'success')
    except Exception as e: flash(f'Error saving configuration: {str(e)}', 'error')
    return redirect(url_for('config_page'))

@app.route('/test_slack')
def test_slack():
    success, message = test_slack_connection()
    if success:
        send_slack_notification("🧪 Test notification from Lightning Detector v1.12")
        flash(f'Slack test successful: {message}', 'success')
    else: flash(f'Slack test failed: {message}', 'error')
    return redirect(url_for('config_page'))

@app.route('/start_monitoring')
def start_monitoring_route():
    with MONITORING_STATE['lock']:
        thread = MONITORING_STATE.get("thread")
        if thread and thread.is_alive():
            flash('Monitoring is already running.', 'info')
        else:
            MONITORING_STATE['stop_event'].clear()
            new_thread = threading.Thread(target=lightning_monitoring, daemon=True)
            MONITORING_STATE['thread'] = new_thread
            new_thread.start()
            flash('Lightning monitoring started!', 'success')
            app.logger.info("Monitoring started via web UI.")
    return redirect(url_for('index'))

@app.route('/stop_monitoring')
def stop_monitoring_route():
    MONITORING_STATE['stop_event'].set()
    cleanup_alert_timers()
    flash('Stop signal sent to monitoring thread.', 'info')
    app.logger.info("Monitoring stop requested via web UI.")
    return redirect(url_for('index'))

@app.route('/test_alerts')
def test_alerts():
    if not app.debug:
        flash('Alert testing is only available in debug mode.', 'warning')
        return redirect(url_for('index'))
    test_type = request.args.get('type', 'warning')
    simulated_energy = get_config_int('ALERTS', 'energy_threshold', 100000) + 1
    critical_distance = get_config_int('ALERTS', 'critical_distance', 10)
    warning_distance = get_config_int('ALERTS', 'warning_distance', 30)
    if test_type == 'warning':
        simulated_distance = (critical_distance + warning_distance) // 2
        check_alert_conditions(simulated_distance, simulated_energy)
        flash(f'Warning alert test triggered with strike at {simulated_distance}km!', 'info')
    elif test_type == 'critical':
        simulated_distance = critical_distance - 1 if critical_distance > 1 else 1
        check_alert_conditions(simulated_distance, simulated_energy)
        flash(f'Critical alert test triggered with strike at {simulated_distance}km!', 'info')
    return redirect(url_for('index'))

@app.route('/reset_alerts')
def reset_alerts():
    cleanup_alert_timers()
    flash('All alert states have been reset.', 'success')
    app.logger.info("Alert states manually reset via web interface.")
    return redirect(url_for('index'))

# --- Main Execution ---
def load_and_configure():
    CONFIG_FILE = 'config.ini'
    if not os.path.exists(CONFIG_FILE):
        print(f"ERROR: Configuration file '{CONFIG_FILE}' not found. Exiting.")
        exit(1)
    CONFIG.read(CONFIG_FILE)
    required_sections = ['SYSTEM', 'SENSOR', 'SLACK', 'ALERTS', 'LOGGING']
    for section in required_sections:
        if not CONFIG.has_section(section):
            CONFIG.add_section(section)
    log_cfg = CONFIG['LOGGING']
    handler = RotatingFileHandler('lightning_detector.log', maxBytes=get_config_int('LOGGING', 'max_file_size', 10) * 1024 * 1024, backupCount=get_config_int('LOGGING', 'backup_count', 5))
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    app.logger.addHandler(handler)
    app.logger.setLevel(log_cfg.get('level', 'INFO').upper())
    app.logger.info("Lightning Detector v1.12 starting up.")

def start_monitoring_on_boot():
    if get_config_boolean('SENSOR', 'auto_start', True):
        with MONITORING_STATE['lock']:
            if not (MONITORING_STATE.get('thread') and MONITORING_STATE['thread'].is_alive()):
                MONITORING_STATE['stop_event'].clear()
                thread = threading.Thread(target=lightning_monitoring, daemon=True)
                MONITORING_STATE['thread'] = thread
                thread.start()
                app.logger.info("Auto-starting lightning monitoring.")

def cleanup_on_exit():
    print("\nShutting down...")
    app.logger.info("Shutdown initiated. Cleaning up resources.")
    MONITORING_STATE['stop_event'].set()
    cleanup_alert_timers()
    with MONITORING_STATE['lock']:
        thread = MONITORING_STATE.get('thread')
        if thread and thread.is_alive():
            thread.join(timeout=5)
    print("Cleanup complete.")

if __name__ == '__main__':
    load_and_configure()
    atexit.register(cleanup_on_exit)
    start_monitoring_on_boot()
    is_debug_mode = get_config_boolean('SYSTEM', 'debug', False)
    if is_debug_mode:
        app.logger.warning("Application is running in DEBUG mode. Do not use in production.")
    app.run(host='0.0.0.0', port=5000, debug=is_debug_mode)
