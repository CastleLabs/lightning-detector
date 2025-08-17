#!/usr/bin/env python3
"""
CJMCU-3935 Lightning Detector Flask Application v2.1 with Imperial Units
Event-Driven & Hardened Architecture - gpiozero Implementation

This application provides a web-based interface for monitoring lightning activity
using the AS3935 sensor chip. It features:
- Event-driven interrupt-based detection (no polling)
- Automatic noise floor adjustment
- Slack notifications for alerts
- Web dashboard with real-time status
- Configurable alert zones (warning/critical)
- Automatic recovery from sensor failures
- Thread-safe operation for reliability
- Production-grade reliability enhancements
- Updated to use gpiozero for improved GPIO handling
- Support for imperial (miles) and metric (km) units

Author: Lightning Detector Project
Version: 2.1-Production-Enhanced-gpiozero-Imperial-FIXED
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
from queue import Queue, Empty

import requests
import spidev
from gpiozero import Device, Button
from gpiozero.pins.pigpio import PiGPIOFactory
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, Response
from werkzeug.serving import WSGIRequestHandler

# Try to use pigpio for better performance, fall back to default if not available
try:
    Device.pin_factory = PiGPIOFactory()
    GPIO_BACKEND = "pigpio"
except Exception:
    # Use default pin factory (RPi.GPIO-based)
    GPIO_BACKEND = "default"

# --- Constants and Enumerations ---
class AlertLevel(Enum):
    """Enumeration for different alert severity levels"""
    WARNING = "warning"
    CRITICAL = "critical"
    ALL_CLEAR = "all_clear"

# --- Unit Conversion Helpers ---
def km_to_miles(km):
    """Convert kilometers to miles"""
    return km * 0.621371

def miles_to_km(miles):
    """Convert miles to kilometers"""
    return miles / 0.621371

def format_distance(km_value, use_imperial=True, include_unit=True):
    """
    Format distance value with appropriate unit

    Args:
        km_value: Distance in kilometers
        use_imperial: If True, convert to miles; if False, keep as km
        include_unit: If True, include unit suffix

    Returns:
        Formatted string with distance and unit
    """
    if use_imperial:
        value = km_to_miles(km_value)
        unit = "mi" if include_unit else ""
        return f"{value:.1f}{' ' + unit if unit else ''}"
    else:
        unit = "km" if include_unit else ""
        return f"{km_value}{' ' + unit if unit else ''}"

def get_distance_unit():
    """Get the configured distance unit (imperial or metric)"""
    return get_config_boolean('DISPLAY', 'use_imperial_units', True)

# --- Rate Limiting Filter for Logging ---
class RateLimitFilter(logging.Filter):
    """Rate limit repetitive log messages to prevent log spam"""
    def __init__(self, rate=10):
        super().__init__()
        self.rate = rate
        self.messages = {}

    def filter(self, record):
        current_time = time.time()
        msg = record.getMessage()

        if msg in self.messages:
            last_time, count = self.messages[msg]
            if current_time - last_time < 60:  # Within 1 minute
                if count >= self.rate:
                    return False  # Suppress
                self.messages[msg] = (last_time, count + 1)
            else:
                self.messages[msg] = (current_time, 1)
        else:
            self.messages[msg] = (current_time, 1)

        return True

# --- Global Configuration and State Management ---
# ConfigParser instance for reading configuration from config.ini
CONFIG = configparser.ConfigParser()

# Main monitoring state - thread-safe dictionary holding all shared state
# This dictionary is protected by the 'lock' member for thread safety
MONITORING_STATE = {
    "lock": threading.Lock(),              # Protects all state modifications
    "stop_event": threading.Event(),       # Signals threads to stop
    "events": deque(maxlen=100),           # Circular buffer of lightning events
    "status": {                            # Current system status
        'last_reading': None,              # ISO timestamp of last sensor reading
        'sensor_active': False,            # Is monitoring thread running?
        'status_message': 'Not started',   # Human-readable status
        'indoor_mode': False,              # Indoor/outdoor mode from config
        'noise_mode': 'Normal',            # Current noise mitigation: Normal/High/Critical
        'sensor_healthy': True,            # Is sensor responding correctly?
        'last_error': None,                # Last error message if any
        'use_imperial': True               # Use imperial units (miles)
    },
    "thread": None,                        # Reference to monitoring thread
    "noise_events": deque(maxlen=50),      # Buffer for counting disturber events
    "noise_revert_timer": None,            # Timer to revert noise floor changes
    "watchdog_thread": None,               # Thread monitoring the monitoring thread
    "last_interrupt_time": 0,              # For interrupt storm detection
    "interrupt_count": 0,                  # Count interrupts for storm detection
    "interrupt_storm_detected": False      # Flag for interrupt storm condition
}

# Alert state management - separate from monitoring state for clarity
ALERT_STATE = {
    "warning_timer": None,                 # Timer for warning zone all-clear
    "critical_timer": None,                # Timer for critical zone all-clear
    "warning_active": False,               # Is warning alert currently active?
    "critical_active": False,              # Is critical alert currently active?
    "last_warning_strike": None,           # Timestamp of last warning zone strike
    "last_critical_strike": None,          # Timestamp of last critical zone strike
    "timer_lock": threading.Lock(),        # Protects timer operations
    "active_timers": []                    # Track all active timers
}

# Slack notification queue for non-blocking alerts
SLACK_QUEUE = Queue(maxsize=100)
SLACK_WORKER_THREAD = None

# Sensor instance and initialization lock
# The lock ensures only one thread can initialize/access the sensor at a time
SENSOR_INIT_LOCK = threading.Lock()
sensor = None  # Global sensor object

# --- Flask Application Setup ---
app = Flask(__name__)
app.secret_key = 'lightning-detector-hardened-v21-production-enhanced-gpiozero-imperial-secret-key'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max request size

# Set request timeout
WSGIRequestHandler.timeout = 30

# --- AS3935 Sensor Driver Class with gpiozero ---
class AS3935LightningDetector:
    """
    Hardened driver for CJMCU-3935 (AS3935) Lightning Detector using gpiozero
    """

    # AS3935 Register addresses (from datasheet)
    REG_AFE_GAIN = 0x00    # Analog Front-End gain and power settings
    REG_PWD = 0x00         # Power down control (shared with AFE_GAIN)
    REG_MIXED_MODE = 0x01  # Noise floor level and watchdog threshold
    REG_SREJ = 0x02        # Spike rejection settings
    REG_LCO_FDIV = 0x03    # Local oscillator frequency settings
    REG_MASK_DIST = 0x03   # Mask disturber events (shared register)
    REG_DISP_LCO = 0x08    # Display oscillator on IRQ pin for tuning
    REG_PRESET = 0x3C      # Preset register for testing

    # Interrupt reason bit masks
    INT_NH = 0x01          # Noise level too high
    INT_D  = 0x04          # Disturber detected
    INT_L  = 0x08          # Lightning detected

    def __init__(self, spi_bus=0, spi_device=0, irq_pin=2):
        """
        Initialize the AS3935 sensor using gpiozero

        Args:
            spi_bus: SPI bus number (0 or 1 on Raspberry Pi)
            spi_device: SPI chip select (0 or 1)
            irq_pin: GPIO pin number for interrupt signal (BCM numbering)
        """
        self.spi = None
        self.irq_pin = irq_pin
        self.irq_button = None
        self.is_initialized = False
        self.original_noise_floor = 0x02  # Default noise floor level
        self.interrupt_callback = None

        # Read sensor-related config (with safe fallbacks)
        try:
            self.active_high = get_config_boolean('SENSOR', 'irq_active_high', True)
            spi_mode = get_config_int('SENSOR', 'spi_mode', 1)
            spi_max_hz = get_config_int('SENSOR', 'spi_max_hz', 2000000)
        except Exception:
            self.active_high = True
            spi_mode = 1
            spi_max_hz = 2000000

        try:
            # Initialize SPI communication
            self.spi = spidev.SpiDev()
            self.spi.open(spi_bus, spi_device)
            # Respect config for SPI mode/speed
            self.spi.max_speed_hz = int(spi_max_hz)
            self.spi.mode = [0, 1, 2, 3][min(max(int(spi_mode), 0), 3)]

            # Configure GPIO for interrupt pin using gpiozero Button
            # Polarity/edge:
            #   active_high=True  -> idle LOW, pulse HIGH  -> pull_up=False, trigger on RISING
            #   active_high=False -> idle HIGH, pulse LOW  -> pull_up=True,  trigger on FALLING
            pull_up = not self.active_high
            self.irq_button = Button(
                pin=self.irq_pin,
                pull_up=pull_up,
                bounce_time=0.002  # short debounce; IRQ is a brief pulse
            )

            app.logger.debug(
                f"IRQ pin {self.irq_pin} initialized with gpiozero Button "
                f"(backend: {GPIO_BACKEND}, active_high={self.active_high}, pull_up={pull_up})"
            )

            # Let the pin settle
            time.sleep(0.1)

            # Log idle state
            idle_pressed = self.irq_button.is_pressed
            # is_pressed meaning depends on pull_up:
            #   pull_up=False (active_high): True -> HIGH, False -> LOW
            #   pull_up=True  (active_low):  True -> LOW,  False -> HIGH
            if pull_up:
                idle_level = "LOW" if idle_pressed else "HIGH"
            else:
                idle_level = "HIGH" if idle_pressed else "LOW"
            app.logger.debug(f"IRQ pin {self.irq_pin} idle state: {idle_level}")

            # Power up and configure the sensor
            self.power_up()
            self.is_initialized = True

        except Exception as e:
            # Clean up on initialization failure
            self.cleanup()
            raise e

    def _write_register(self, reg, value, retries=3):
        """
        Write a value to a sensor register with retry logic

        Args:
            reg: Register address (0x00-0x3F)
            value: 8-bit value to write
            retries: Number of retry attempts on failure
        """
        if not self.spi:
            return

        for attempt in range(retries):
            try:
                # AS3935 expects [register_address, data_byte]
                self.spi.xfer2([reg, value])
                return
            except IOError as e:
                if attempt == retries - 1:
                    app.logger.error(f"SPI write failed after {retries} attempts: {e}")
                    raise
                time.sleep(0.001)  # Brief delay before retry

    def _read_register(self, reg, retries=3):
        """
        Read a value from a sensor register with retry logic

        Args:
            reg: Register address (0x00-0x3F)
            retries: Number of retry attempts on failure

        Returns:
            8-bit register value
        """
        if not self.spi:
            return 0

        for attempt in range(retries):
            try:
                # AS3935 read: set bit 6 of address byte, then read response
                result = self.spi.xfer2([reg | 0x40, 0x00])
                return result[1]
            except IOError as e:
                if attempt == retries - 1:
                    app.logger.error(f"SPI read failed after {retries} attempts: {e}")
                    raise
                time.sleep(0.001)
        return 0

    def power_up(self):
        """
        Initialize and calibrate the sensor according to datasheet specifications
        FIXED VERSION - Properly clears interrupts and ensures detection works
        """
        try:
            # CRITICAL FIX 1: Send direct reset command FIRST
            app.logger.info("Performing AS3935 reset...")
            self._write_register(0x3C, 0x96)  # Direct reset command
            time.sleep(0.002)

            # CRITICAL FIX 2: Clear power down bit
            self._write_register(0x00, 0x00)  # Clear ALL bits including PWD
            time.sleep(0.002)

            # CRITICAL FIX 3: Clear interrupts MULTIPLE times (the sensor can be stubborn)
            app.logger.info("Clearing any stuck interrupts...")
            for i in range(10):  # More aggressive clearing
                _ = self._read_register(0x03)
                time.sleep(0.002)

            # Get configuration
            try:
                is_indoor = get_config_boolean('SENSOR', 'indoor', True)  # True for BBQ lighter testing
                sensitivity = CONFIG.get('SENSOR', 'sensitivity', fallback='high')
                if sensitivity is None:
                    sensitivity = 'high'
            except Exception as config_error:
                app.logger.warning(f"Config access error in power_up: {config_error}. Using defaults.")
                is_indoor = True
                sensitivity = 'high'

            # Configure AFE Gain for indoor/outdoor
            if is_indoor:
                # Indoor: AFE_GB=10010 (18x gain) - MORE SENSITIVE
                afe_gain = 0b00100100  # Corrected value for indoor
            else:
                # Outdoor: AFE_GB=01110 (14x gain)
                afe_gain = 0b00011100

            self._write_register(0x00, afe_gain)
            time.sleep(0.002)

            # Sensitivity presets
            if sensitivity == 'high':
                nf_lev = 0x00  # Minimum noise floor (most sensitive)
                wdth = 0x00    # Minimum watchdog threshold
                srej = 0x00    # Minimum spike rejection
            elif sensitivity == 'medium':
                nf_lev = 0x02
                wdth = 0x01
                srej = 0x02
            else:  # low
                nf_lev = 0x04
                wdth = 0x03
                srej = 0x03

            self.original_noise_floor = nf_lev

            # Set noise floor and watchdog
            reg01_value = (nf_lev << 4) | wdth
            self._write_register(0x01, reg01_value)
            time.sleep(0.002)

            # Set spike rejection (register 0x02)
            self._write_register(0x02, srej << 4)
            time.sleep(0.002)

            # CRITICAL FIX 4: Ensure MASK_DIST bit is CLEARED (bit 5 of register 0x03)
            reg03 = self._read_register(0x03)
            reg03 = reg03 & ~(1 << 5)  # Clear bit 5 (MASK_DIST)

            # FIXED: DO NOT MASK INT_NH - We want to see ALL interrupts including noise
            # This was preventing detection of piezo lighters
            app.logger.info("All interrupts enabled (including INT_NH for noise detection)")

            self._write_register(0x03, reg03)
            time.sleep(0.002)

            # Verify MASK_DIST is cleared
            reg03_check = self._read_register(0x03)
            mask_dist = (reg03_check >> 5) & 0x01
            if mask_dist:
                app.logger.error("WARNING: MASK_DIST bit is still set! Disturbers will be hidden!")
            else:
                app.logger.info("MASK_DIST cleared - disturbers will trigger interrupts")

            # Calibrate oscillators (briefly display LCO)
            self._write_register(0x08, 0x80)  # DISP_LCO
            time.sleep(0.002)
            self._write_register(0x08, 0x00)  # Clear DISP_LCO
            time.sleep(0.002)

            # CRITICAL FIX 5: Final interrupt clear
            for _ in range(5):
                _ = self._read_register(0x03)
                time.sleep(0.002)

            # Update global status
            try:
                with MONITORING_STATE['lock']:
                    MONITORING_STATE['status']['indoor_mode'] = is_indoor
                    MONITORING_STATE['status']['sensor_healthy'] = True
                    MONITORING_STATE['status']['use_imperial'] = get_distance_unit()
            except Exception as state_error:
                app.logger.warning(f"State update error in power_up: {state_error}")

            # Log final configuration
            final_regs = {
                "0x00 (AFE/PWD)": f"0x{self._read_register(0x00):02X}",
                "0x01 (NF/WDTH)": f"0x{self._read_register(0x01):02X}",
                "0x02 (SREJ)": f"0x{self._read_register(0x02):02X}",
                "0x03 (INT/MASK)": f"0x{self._read_register(0x03):02X}",
            }
            app.logger.info(
                f"AS3935 initialized - Mode: {'Indoor' if is_indoor else 'Outdoor'}, "
                f"Sensitivity: {sensitivity}, IRQ active_high={self.active_high}"
            )
            app.logger.info(f"Final registers: {final_regs}")

            # Check if interrupts are working
            int_reg = self._read_register(0x03) & 0x0F
            app.logger.info(f"Current interrupt status: 0x{int_reg:02X}")

        except Exception as e:
            try:
                with MONITORING_STATE['lock']:
                    MONITORING_STATE['status']['sensor_healthy'] = False
                    MONITORING_STATE['status']['last_error'] = str(e)
            except:
                pass
            raise

    def set_interrupt_callback(self, callback):
        """
        Set the interrupt callback function using gpiozero

        Args:
            callback: Function to call when interrupt occurs
        """
        if not self.irq_button:
            raise Exception("IRQ button not initialized")

        self.interrupt_callback = callback

        # Edge selection based on polarity
        if self.active_high:
            # Idle LOW -> pulse HIGH -> rising edge -> when_pressed
            self.irq_button.when_pressed = self._interrupt_wrapper
            self.irq_button.when_released = None
            edge_name = "RISING"
        else:
            # Idle HIGH -> pulse LOW -> falling edge -> when_pressed (with pull_up=True)
            self.irq_button.when_pressed = self._interrupt_wrapper
            self.irq_button.when_released = None
            edge_name = "FALLING"

        app.logger.info(f"Interrupt callback set for GPIO{self.irq_pin} using gpiozero ({edge_name})")

    def _interrupt_wrapper(self):
        """
        Wrapper for the interrupt callback to match expected signature
        """
        if self.interrupt_callback:
            # Call the callback with the pin number (to match RPi.GPIO behavior)
            self.interrupt_callback(self.irq_pin)

    def remove_interrupt_callback(self):
        """
        Remove the interrupt callback
        """
        if self.irq_button:
            self.irq_button.when_pressed = None
            self.irq_button.when_released = None
            app.logger.debug(f"Removed interrupt callback from GPIO{self.irq_pin}")

    def set_noise_floor(self, level):
        """
        Dynamically adjust the noise floor level

        Args:
            level: Noise floor level (0-7, where 7 is least sensitive)
        """
        if not (0x00 <= level <= 0x07):
            app.logger.error(f"Invalid noise floor level: {level}. Must be 0-7.")
            return

        try:
            # Read current register to preserve watchdog threshold
            current_reg_val = self._read_register(self.REG_MIXED_MODE)
            preserved_wdth = current_reg_val & 0x0F

            # Update noise floor while preserving watchdog
            new_reg_val = (level << 4) | preserved_wdth
            self._write_register(self.REG_MIXED_MODE, new_reg_val)

            app.logger.info(f"Noise floor dynamically set to level {level}")

        except IOError as e:
            app.logger.error(f"SPI Error setting noise floor: {e}")
            with MONITORING_STATE['lock']:
                MONITORING_STATE['status']['sensor_healthy'] = False
                MONITORING_STATE['status']['last_error'] = str(e)

    def get_interrupt_reason(self):
        """Read interrupt status register to determine interrupt cause"""
        return self._read_register(0x03) & 0x0F

    def get_lightning_distance(self):
        """
        Read estimated distance to lightning strike

        Returns:
            Distance in km (1-63), or 0x3F for out of range
        """
        return self._read_register(0x07) & 0x3F

    def get_lightning_energy(self):
        """
        Read lightning energy value (arbitrary units)

        Returns:
            20-bit energy value
        """
        lsb = self._read_register(0x04)
        msb = self._read_register(0x05)
        mmsb = self._read_register(0x06) & 0x1F
        return (mmsb << 16) | (msb << 8) | lsb

    def verify_spi_connection(self):
        """
        Verify SPI connection is working properly
        
        Returns:
            True if SPI communication is working, False otherwise
        """
        try:
            # Test by reading a register that should have a known value
            # Register 0x00 should never be 0xFF in normal operation
            test_read = self._read_register(0x00)
            if test_read == 0xFF:  # Likely SPI communication failure
                return False
            
            # Try reading it again to verify consistency
            test_read2 = self._read_register(0x00)
            return test_read == test_read2
        except Exception as e:
            app.logger.error(f"SPI verification error: {e}")
            return False

    def cleanup(self):
        """
        Clean up sensor resources safely using gpiozero
        """
        try:
            # Clean up interrupt callback first
            if self.irq_button:
                self.remove_interrupt_callback()
                self.irq_button.close()  # gpiozero cleanup
                self.irq_button = None
                app.logger.debug(f"gpiozero Button for GPIO{self.irq_pin} closed")

            # Clean up SPI
            if self.spi:
                self.spi.close()
                self.spi = None

            app.logger.info(f"Sensor resources cleaned up (gpiozero)")

        except Exception as e:
            app.logger.error(f"Error during hardware cleanup: {e}")

# --- Configuration Helper Functions ---
def get_config_int(section, key, fallback):
    """
    Safely retrieve an integer value from configuration

    Args:
        section: INI file section name
        key: Configuration key
        fallback: Default value if not found or invalid

    Returns:
        Integer value from config or fallback
    """
    try:
        value = CONFIG.get(section, key, fallback=None)
        if value is None:
            return fallback
        return int(value)
    except (ValueError, configparser.NoOptionError, configparser.NoSectionError, TypeError):
        if 'app' in globals() and hasattr(app, 'logger'):
            app.logger.warning(f"Invalid or missing value for '{key}' in [{section}]. Using fallback: {fallback}.")
        return fallback

def get_config_float(section, key, fallback):
    """Safely retrieve a float value from configuration"""
    try:
        value = CONFIG.get(section, key, fallback=None)
        if value is None:
            return fallback
        return float(value)
    except (ValueError, configparser.NoOptionError, configparser.NoSectionError, TypeError):
        if 'app' in globals() and hasattr(app, 'logger'):
            app.logger.warning(f"Invalid or missing value for '{key}' in [{section}]. Using fallback: {fallback}.")
        return fallback

def get_config_boolean(section, key, fallback):
    """Safely retrieve a boolean value from configuration"""
    try:
        value = CONFIG.get(section, key, fallback=None)
        if value is None:
            return fallback
        # Handle various boolean representations
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ('true', 'yes', '1', 'on')
        return bool(value)
    except (ValueError, configparser.NoOptionError, configparser.NoSectionError, TypeError):
        if 'app' in globals() and hasattr(app, 'logger'):
            app.logger.warning(f"Invalid or missing value for '{key}' in [{section}]. Using fallback: {fallback}.")
        return fallback

def validate_config():
    """
    Validate critical configuration values

    Returns:
        True if configuration is valid, False otherwise
    """
    errors = []
    warnings = []

    try:
        # Get unit preference
        use_imperial = get_config_boolean('DISPLAY', 'use_imperial_units', True)

        # Validate distance settings with safe comparisons
        critical_km = get_config_int('ALERTS', 'critical_distance', 10)
        warning_km = get_config_int('ALERTS', 'warning_distance', 30)

        # Debug logging
        if 'app' in globals() and hasattr(app, 'logger'):
            if use_imperial:
                app.logger.debug(f"Config validation: critical={format_distance(critical_km, True)}, warning={format_distance(warning_km, True)}")
            else:
                app.logger.debug(f"Config validation: critical={critical_km}km, warning={warning_km}km")

        # Ensure we have valid integers
        if critical_km is None:
            critical_km = 10
            warnings.append("Critical distance was None, using default 10km")
        if warning_km is None:
            warning_km = 30
            warnings.append("Warning distance was None, using default 30km")

        # Convert to int to be absolutely sure
        try:
            critical_km = int(critical_km)
            warning_km = int(warning_km)
        except (ValueError, TypeError) as e:
            errors.append(f"Distance values are not valid integers: critical={critical_km}, warning={warning_km}")
            if 'app' in globals() and hasattr(app, 'logger'):
                app.logger.error(f"Distance conversion error: {e}")
            return False

        if critical_km >= warning_km:
            errors.append(f"Critical distance must be less than warning distance ({format_distance(critical_km, use_imperial)} >= {format_distance(warning_km, use_imperial)})")

        if critical_km < 1 or critical_km > 63:  # AS3935 max distance
            errors.append(f"Critical distance must be between {format_distance(1, use_imperial)} and {format_distance(63, use_imperial)}")

        if warning_km < 1 or warning_km > 63:
            errors.append(f"Warning distance must be between {format_distance(1, use_imperial)} and {format_distance(63, use_imperial)}")

        # Validate SPI settings
        spi_bus = get_config_int('SENSOR', 'spi_bus', 0)
        if spi_bus is None:
            spi_bus = 0
            warnings.append("SPI bus was None, using default 0")

        try:
            spi_bus = int(spi_bus)
        except (ValueError, TypeError):
            errors.append(f"SPI bus is not a valid integer: {spi_bus}")
            return False

        if spi_bus not in [0, 1]:
            errors.append("SPI bus must be 0 or 1")

        # Validate GPIO pin
        irq_pin = get_config_int('SENSOR', 'irq_pin', 2)
        if irq_pin is None:
            irq_pin = 2
            warnings.append("IRQ pin was None, using default 2")

        try:
            irq_pin = int(irq_pin)
        except (ValueError, TypeError):
            errors.append(f"IRQ pin is not a valid integer: {irq_pin}")
            return False

        if irq_pin < 0 or irq_pin > 27:  # BCM pin range
            errors.append("IRQ pin must be between 0 and 27")

        # Check for reserved pins
        reserved_pins = [0, 1, 14, 15]  # UART pins
        if irq_pin in reserved_pins:
            warnings.append(f"IRQ pin {irq_pin} may conflict with system functions")

        # Validate noise handling settings
        if get_config_boolean('NOISE_HANDLING', 'enabled', True):
            event_threshold = get_config_int('NOISE_HANDLING', 'event_threshold', 15)
            if event_threshold is None:
                event_threshold = 15
                warnings.append("Event threshold was None, using default 15")

            try:
                event_threshold = int(event_threshold)
            except (ValueError, TypeError):
                errors.append(f"Event threshold is not a valid integer: {event_threshold}")
                return False

            if event_threshold < 5:
                warnings.append("Event threshold < 5 may cause frequent noise floor changes")
            elif event_threshold > 50:
                warnings.append("Event threshold > 50 may not respond to noise quickly enough")

            noise_floor = get_config_int('NOISE_HANDLING', 'raised_noise_floor_level', 5)
            if noise_floor is None:
                noise_floor = 5
                warnings.append("Noise floor level was None, using default 5")

            try:
                noise_floor = int(noise_floor)
            except (ValueError, TypeError):
                errors.append(f"Noise floor level is not a valid integer: {noise_floor}")
                return False

            if noise_floor < 0 or noise_floor > 7:
                errors.append("Raised noise floor level must be between 0 and 7")

    except Exception as e:
        errors.append(f"Configuration validation error: {str(e)}")
        if 'app' in globals() and hasattr(app, 'logger'):
            app.logger.error(f"Exception during config validation: {e}", exc_info=True)

    # Log warnings and errors safely
    if 'app' in globals() and hasattr(app, 'logger'):
        for warning in warnings:
            app.logger.warning(f"Configuration warning: {warning}")

        for error in errors:
            app.logger.error(f"Configuration error: {error}")
    else:
        # Fallback logging if app.logger not available
        for warning in warnings:
            print(f"Configuration warning: {warning}")
        for error in errors:
            print(f"Configuration error: {error}")

    return len(errors) == 0

# --- Sensor Initialization and Management ---
def initialize_sensor_with_retry(max_retries=5, retry_delay=5):
    """
    Initialize sensor with exponential backoff retry logic

    Args:
        max_retries: Maximum number of initialization attempts
        retry_delay: Base delay between retries (exponentially increased)

    Returns:
        True if initialization successful, False otherwise
    """
    global sensor

    for attempt in range(max_retries):
        try:
            with SENSOR_INIT_LOCK:
                # Clean up any existing sensor instance
                if sensor:
                    sensor.cleanup()
                    sensor = None

                # Get config values with detailed logging
                spi_bus = get_config_int('SENSOR', 'spi_bus', 0)
                spi_device = get_config_int('SENSOR', 'spi_device', 0)
                irq_pin = get_config_int('SENSOR', 'irq_pin', 2)

                app.logger.debug(f"Config values: spi_bus={spi_bus} (type: {type(spi_bus)}), "
                                 f"spi_device={spi_device} (type: {type(spi_device)}), "
                                 f"irq_pin={irq_pin} (type: {type(irq_pin)})")

                # Ensure all values are valid integers
                if spi_bus is None:
                    spi_bus = 0
                if spi_device is None:
                    spi_device = 0
                if irq_pin is None:
                    irq_pin = 2

                # Convert to int if they're not already
                spi_bus = int(spi_bus)
                spi_device = int(spi_device)
                irq_pin = int(irq_pin)

                app.logger.debug(f"Final values: spi_bus={spi_bus}, spi_device={spi_device}, irq_pin={irq_pin}")

                # Create new sensor instance
                sensor = AS3935LightningDetector(
                    spi_bus=spi_bus,
                    spi_device=spi_device,
                    irq_pin=irq_pin
                )

                # Verify sensor is responsive by reading a register
                test_value = sensor._read_register(0x00)
                app.logger.info(f"Sensor initialized successfully with gpiozero (test read: {test_value:#04x})")

                # Update global status
                with MONITORING_STATE['lock']:
                    MONITORING_STATE['status']['sensor_active'] = True
                    MONITORING_STATE['status']['sensor_healthy'] = True
                    MONITORING_STATE['status']['status_message'] = f"Monitoring (Event-Driven, {GPIO_BACKEND})"
                    MONITORING_STATE['status']['last_error'] = None
                    MONITORING_STATE['status']['use_imperial'] = get_distance_unit()

                return True

        except Exception as e:
            app.logger.error(f"Sensor init attempt {attempt + 1}/{max_retries} failed: {e}")
            # Add traceback for better debugging
            import traceback
            app.logger.error(f"Full traceback: {traceback.format_exc()}")

            # Update status with failure information
            with MONITORING_STATE['lock']:
                MONITORING_STATE['status']['sensor_active'] = False
                MONITORING_STATE['status']['sensor_healthy'] = False
                MONITORING_STATE['status']['last_error'] = str(e)
                MONITORING_STATE['status']['status_message'] = f"Init failed (attempt {attempt + 1})"

            # Wait before retry with exponential backoff
            if attempt < max_retries - 1:
                delay = retry_delay * (2 ** attempt)  # Exponential backoff
                app.logger.info(f"Waiting {delay}s before retry...")

                # Interruptible sleep
                for _ in range(delay * 10):
                    if MONITORING_STATE['stop_event'].is_set():
                        return False
                    time.sleep(0.1)

    # All retries exhausted
    with MONITORING_STATE['lock']:
        MONITORING_STATE['status']['status_message'] = "Fatal: Max retries exceeded"

    return False

def perform_sensor_health_check():
    """
    Perform a comprehensive health check on the sensor

    Returns:
        True if sensor is healthy, False otherwise
    """
    try:
        with SENSOR_INIT_LOCK:
            if not sensor or not sensor.is_initialized:
                return False

            # Read and verify power register
            pwd_reg = sensor._read_register(0x00)
            if (pwd_reg & 0x01) != 0:  # Check if powered down
                app.logger.warning(f"Sensor appears to be powered down: {pwd_reg:#04x}")
                return False

            # Enhanced SPI verification
            if not sensor.verify_spi_connection():
                return False

            # Try reading multiple registers to ensure communication
            try:
                sensor._read_register(0x01)  # Noise floor register
                sensor._read_register(0x02)  # Spike rejection register
            except:
                return False

            # Check if gpiozero button is still functional
            if sensor.irq_button and hasattr(sensor.irq_button, 'is_pressed'):
                try:
                    # Just checking if we can read the pin state
                    _ = sensor.irq_button.is_pressed
                except:
                    app.logger.warning("GPIO pin state unreadable via gpiozero")
                    return False

            # Update status on success
            with MONITORING_STATE['lock']:
                MONITORING_STATE['status']['sensor_healthy'] = True
                MONITORING_STATE['status']['last_error'] = None

            return True

    except Exception as e:
        app.logger.error(f"Sensor health check failed: {e}")
        with MONITORING_STATE['lock']:
            MONITORING_STATE['status']['sensor_healthy'] = False
            MONITORING_STATE['status']['last_error'] = str(e)
        return False

# --- Lightning Detection and Event Handling ---
def decode_interrupt(int_val):
    """Decode interrupt value to human-readable string"""
    if int_val == 0x00:
        return "No interrupt"
    elif int_val & 0x08:
        return "Lightning detected!"
    elif int_val & 0x04:
        return "Disturber detected"
    elif int_val & 0x01:
        return "Noise too high"
    else:
        return f"Unknown: 0x{int_val:02X}"

@app.route('/full_diagnostic')
def full_diagnostic():
    """Complete diagnostic of the AS3935 sensor and GPIO"""
    if not sensor:
        return jsonify({"error": "Sensor not initialized"}), 500

    results = {
        "timestamp": datetime.now().isoformat(),
        "config": {
            "irq_pin": sensor.irq_pin,
            "indoor": get_config_boolean('SENSOR', 'indoor', True),
            "sensitivity": CONFIG.get('SENSOR', 'sensitivity', fallback='high'),
            "irq_active_high": getattr(sensor, "active_high", True)
        }
    }

    try:
        # 1. Test GPIO pin state
        pull_up = not sensor.active_high
        pressed = sensor.irq_button.is_pressed
        if pull_up:
            gpio_state = "LOW" if pressed else "HIGH"
            expected_idle = "HIGH when idle (active_low IRQ)"
        else:
            gpio_state = "HIGH" if pressed else "LOW"
            expected_idle = "LOW when idle (active_high IRQ)"
        results["gpio_state"] = gpio_state
        results["gpio_expected"] = expected_idle

        # 2. Read ALL registers
        registers = {}
        for addr in range(0x00, 0x09):
            val = sensor._read_register(addr)
            registers[f"0x{addr:02X}"] = {
                "hex": f"0x{val:02X}",
                "bin": f"0b{val:08b}"
            }

            # Decode important registers
            if addr == 0x00:
                registers[f"0x{addr:02X}"]["decoded"] = {
                    "PWD": "POWERED_DOWN" if (val & 0x01) else "ACTIVE",
                    "AFE_GB": (val >> 1) & 0x1F
                }
            elif addr == 0x03:
                int_val = val & 0x0F
                mask_dist = (val >> 5) & 0x01
                registers[f"0x{addr:02X}"]["decoded"] = {
                    "INT": f"0x{int_val:02X}",
                    "MASK_DIST": "MASKED (BAD!)" if mask_dist else "ENABLED (GOOD)"
                }

        results["registers"] = registers

        # 3. Try to generate a disturber (sensitivity boost)
        app.logger.info("Attempting to generate disturber...")
        original_nf = sensor._read_register(0x01)

        # Set ultra-sensitive settings temporarily
        sensor._write_register(0x01, 0x00)  # Min noise floor
        sensor._write_register(0x02, 0x00)  # Min spike rejection
        time.sleep(0.5)

        # Check for any interrupt
        int_check = sensor._read_register(0x03) & 0x0F
        results["disturber_test"] = {
            "interrupt_generated": int_check != 0,
            "interrupt_value": f"0x{int_check:02X}"
        }

        # Restore settings
        sensor._write_register(0x01, original_nf)

        # Clear interrupts
        for _ in range(5):
            sensor._read_register(0x03)

        results["status"] = "Diagnostic complete"
        return jsonify(results)

    except Exception as e:
        results["error"] = str(e)
        return jsonify(results), 500

@app.route('/force_trigger')
def force_trigger():
    """Force a test interrupt by manipulating the sensor"""
    if not sensor:
        return jsonify({"error": "Sensor not initialized"}), 500

    try:
        app.logger.warning("=== FORCING TEST TRIGGER ===")

        # Save current settings
        orig_01 = sensor._read_register(0x01)
        orig_02 = sensor._read_register(0x02)

        # Set to absolute maximum sensitivity
        sensor._write_register(0x01, 0x00)  # NF_LEV=0, WDTH=0
        sensor._write_register(0x02, 0x00)  # SREJ=0, MIN_NUM_LIGH=0

        # Clear MASK_DIST to ensure disturbers show
        reg03 = sensor._read_register(0x03)
        reg03 = reg03 & ~(1 << 5)
        sensor._write_register(0x03, reg03)

        # Generate some SPI noise to trigger disturber
        for _ in range(10):
            sensor._read_register(0x00)
            time.sleep(0.001)

        # Wait for interrupt
        time.sleep(0.5)

        # Check interrupt register
        int_val = sensor._read_register(0x03) & 0x0F

        # Restore settings
        sensor._write_register(0x01, orig_01)
        sensor._write_register(0x02, orig_02)

        result = {
            "interrupt_detected": int_val != 0,
            "interrupt_value": f"0x{int_val:02X}",
            "message": "Check logs for interrupt messages"
        }

        if int_val == 0x04:
            result["type"] = "Disturber detected!"
        elif int_val == 0x08:
            result["type"] = "Lightning detected!"
        elif int_val == 0x01:
            result["type"] = "Noise too high"

        app.logger.warning(f"=== FORCE TRIGGER RESULT: {result} ===")
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/monitor_interrupts')
def monitor_interrupts():
    """Monitor interrupt register in real-time"""
    if not sensor:
        return jsonify({"error": "Sensor not initialized"}), 500

    results = []
    for i in range(10):
        int_val = sensor._read_register(0x03) & 0x0F
        results.append({
            "iteration": i,
            "interrupt": f"0x{int_val:02X}",
            "decoded": decode_interrupt(int_val)
        })
        time.sleep(0.5)

    return jsonify(results)

@app.route('/test_piezo')
def test_piezo():
    """Special test mode for piezo lighter detection (moderate sensitivity)"""
    if not sensor:
        return jsonify({"error": "Sensor not initialized"}), 500

    try:
        app.logger.warning("=== PIEZO TEST MODE (moderate sensitivity) ===")

        # Save current settings
        orig_00 = sensor._read_register(0x00)
        orig_01 = sensor._read_register(0x01)
        orig_02 = sensor._read_register(0x02)
        orig_03 = sensor._read_register(0x03)

        # 1) Use OUTDOOR AFE (lower gain) during the test
        sensor._write_register(0x00, 0b00011100)
        time.sleep(0.002)

        # 2) Moderate sensitivity: NF_LEV=0x02, WDTH=0x02
        sensor._write_register(0x01, (0x02 << 4) | 0x02)
        time.sleep(0.002)

        # 3) Moderate spike rejection: SREJ=0x02
        sensor._write_register(0x02, (0x02 << 4) | 0x00)
        time.sleep(0.002)

        # 4) Ensure MASK_DIST is cleared but preserve other bits
        reg03 = sensor._read_register(0x03)
        reg03 &= ~(1 << 5)  # clear MASK_DIST
        sensor._write_register(0x03, reg03)
        time.sleep(0.002)

        # 5) Aggressively clear any pending interrupts
        for _ in range(6):
            _ = sensor._read_register(0x03)
            time.sleep(0.002)

        app.logger.warning("Ready: click piezo within 5–10 cm of the board for 12 seconds")

        results = {"test_duration": "12 seconds", "detections": []}

        # Poll faster for 12 seconds
        for i in range(120):
            int_val = sensor._read_register(0x03) & 0x0F
            if int_val:
                det = {
                    "time": f"{i*0.1:.1f}s",
                    "interrupt": f"0x{int_val:02X}",
                    "type": []
                }
                if int_val & 0x08:
                    det["type"].append("LIGHTNING!")
                    dist = sensor.get_lightning_distance()
                    energy = sensor.get_lightning_energy()
                    det["distance_km"] = dist
                    det["energy"] = energy
                if int_val & 0x04:
                    det["type"].append("Disturber")
                if int_val & 0x01:
                    det["type"].append("Noise")

                results["detections"].append(det)

                # Clear latched INT
                _ = sensor._read_register(0x03)

            time.sleep(0.1)

        # Restore original registers
        sensor._write_register(0x00, orig_00)
        sensor._write_register(0x01, orig_01)
        sensor._write_register(0x02, orig_02)
        sensor._write_register(0x03, orig_03)

        app.logger.warning(f"=== PIEZO TEST COMPLETE: {len(results['detections'])} detections ===")
        return jsonify(results)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

def handle_lightning_event():
    """
    Process a lightning detection event

    This function reads the distance and energy values, checks alert
    conditions, logs the event, and sends notifications if needed.
    """
    try:
        # Read lightning parameters
        distance_km = sensor.get_lightning_distance()
        energy = sensor.get_lightning_energy()

        # Get unit preference
        use_imperial = get_distance_unit()

        # Validate readings
        if distance_km == 0x3F:  # Out of range indicator
            max_dist = format_distance(63, use_imperial)
            app.logger.warning(f"Lightning detected but out of range (>{max_dist}), energy: {energy}")
            return

        if distance_km == 0:  # Invalid reading
            app.logger.warning("Lightning detected with invalid distance (0)")
            return

        # Check if this event should trigger alerts
        alert_result = check_alert_conditions(distance_km, energy)

        # Create event record with both units
        event = {
            'timestamp': datetime.now().isoformat(),
            'distance_km': distance_km,
            'distance_display': format_distance(distance_km, use_imperial),
            'energy': energy,
            'energy_formatted': f"{energy:,}",
            'alert_sent': alert_result.get('send_alert', False),
            'alert_level': alert_result.get('level').value if alert_result.get('level') else None
        }

        # Store event in circular buffer
        with MONITORING_STATE['lock']:
            MONITORING_STATE['events'].append(event)
            MONITORING_STATE['status']['sensor_healthy'] = True

        # Log with appropriate units
        distance_str = format_distance(distance_km, use_imperial)
        app.logger.info(f"⚡ Lightning detected: {distance_str}, energy: {energy}")

        # Send alerts if needed
        if alert_result.get('send_alert'):
            level = alert_result.get('level')
            if level == AlertLevel.CRITICAL:
                send_slack_notification(
                    f"🚨 CRITICAL: Lightning strike detected! Distance: {distance_str}",
                    distance_km, energy, level
                )
            elif level == AlertLevel.WARNING:
                send_slack_notification(
                    f"⚠️ WARNING: Lightning detected. Distance: {distance_str}",
                    distance_km, energy, level
                )

    except Exception as e:
        app.logger.error(f"Error handling lightning event: {e}")
        raise

# --- Alert System Functions ---
def check_alert_conditions(distance_km, energy):
    """
    Determine if a lightning event should trigger alerts

    Args:
        distance_km: Distance to strike in kilometers
        energy: Energy level of strike

    Returns:
        Dictionary with 'send_alert' boolean and 'level' AlertLevel enum
    """
    with ALERT_STATE["timer_lock"]:
        now = datetime.now()
        should_send_alert = False
        alert_level = None

        # Check energy threshold
        energy_threshold = get_config_int('ALERTS', 'energy_threshold', 100000)
        if energy < energy_threshold:
            return {"send_alert": False, "level": None}

        # Get configured distances (stored in km in config)
        critical_distance_km = get_config_int('ALERTS', 'critical_distance', 10)
        warning_distance_km = get_config_int('ALERTS', 'warning_distance', 30)

        # Check for critical alert
        if distance_km <= critical_distance_km:
            ALERT_STATE["last_critical_strike"] = now

            # Send alert if this is the first critical strike
            if not ALERT_STATE["critical_active"]:
                ALERT_STATE["critical_active"] = True
                should_send_alert = True
                alert_level = AlertLevel.CRITICAL

                # Cancel warning state if active
                ALERT_STATE["warning_active"] = False

            # Reset or start all-clear timer
            schedule_all_clear_message(AlertLevel.CRITICAL)

        # Check for warning alert (only if not in critical zone)
        elif distance_km <= warning_distance_km and not ALERT_STATE["critical_active"]:
            ALERT_STATE["last_warning_strike"] = now

            # Send alert if this is the first warning strike
            if not ALERT_STATE["warning_active"]:
                ALERT_STATE["warning_active"] = True
                should_send_alert = True
                alert_level = AlertLevel.WARNING

            # Reset or start all-clear timer
            schedule_all_clear_message(AlertLevel.WARNING)

        return {"send_alert": should_send_alert, "level": alert_level}

def schedule_all_clear_message(alert_level):
    """
    Schedule an all-clear message after no activity for configured time

    Args:
        alert_level: AlertLevel enum indicating which zone to monitor
    """
    delay_minutes = get_config_int('ALERTS', 'all_clear_timer', 15)
    use_imperial = get_distance_unit()

    def send_all_clear():
        """Timer callback to send all-clear notification"""
        # Check if monitoring is still active
        if MONITORING_STATE['stop_event'].is_set():
            return

        with ALERT_STATE["timer_lock"]:
            now = datetime.now()

            # Handle warning zone all-clear
            if alert_level == AlertLevel.WARNING and ALERT_STATE["warning_active"]:
                # Verify enough time has passed since last strike
                if ALERT_STATE["warning_timer"] and ALERT_STATE["last_warning_strike"]:
                    if (now - ALERT_STATE["last_warning_strike"]) >= timedelta(minutes=delay_minutes):
                        warning_dist_km = get_config_int('ALERTS', 'warning_distance', 30)
                        warning_dist_str = format_distance(warning_dist_km, use_imperial)
                        send_slack_notification(
                            f"🟢 All Clear: No lightning detected within "
                            f"{warning_dist_str} for {delay_minutes} minutes.",
                            alert_level=AlertLevel.ALL_CLEAR,
                            previous_level=AlertLevel.WARNING
                        )
                        ALERT_STATE["warning_active"] = False
                        ALERT_STATE["warning_timer"] = None

            # Handle critical zone all-clear
            elif alert_level == AlertLevel.CRITICAL and ALERT_STATE["critical_active"]:
                if ALERT_STATE["critical_timer"] and ALERT_STATE["last_critical_strike"]:
                    if (now - ALERT_STATE["last_critical_strike"]) >= timedelta(minutes=delay_minutes):
                        critical_dist_km = get_config_int('ALERTS', 'critical_distance', 10)
                        critical_dist_str = format_distance(critical_dist_km, use_imperial)
                        send_slack_notification(
                            f"🟢 All Clear: No lightning detected within "
                            f"{critical_dist_str} for {delay_minutes} minutes.",
                            alert_level=AlertLevel.ALL_CLEAR,
                            previous_level=AlertLevel.CRITICAL
                        )
                        ALERT_STATE["critical_active"] = False
                        ALERT_STATE["critical_timer"] = None

    # Cancel existing timer if present
    with ALERT_STATE["timer_lock"]:
        if alert_level == AlertLevel.WARNING and ALERT_STATE["warning_timer"]:
            ALERT_STATE["warning_timer"].cancel()
        elif alert_level == AlertLevel.CRITICAL and ALERT_STATE["critical_timer"]:
            ALERT_STATE["critical_timer"].cancel()

        # Clean up dead timers from tracking list
        ALERT_STATE["active_timers"] = [t for t in ALERT_STATE["active_timers"] if t.is_alive()]

        # Create and start new timer
        timer = threading.Timer(delay_minutes * 60, send_all_clear)
        timer.daemon = True
        timer.start()

        # Track new timer
        ALERT_STATE["active_timers"].append(timer)

        # Log if too many timers
        if len(ALERT_STATE['active_timers']) > 10:
            app.logger.warning(f"High number of active timers: {len(ALERT_STATE['active_timers'])}")

        # Store timer reference
        if alert_level == AlertLevel.WARNING:
            ALERT_STATE["warning_timer"] = timer
        elif alert_level == AlertLevel.CRITICAL:
            ALERT_STATE["critical_timer"] = timer

def cleanup_alert_timers():
    """Cancel all active alert timers during shutdown"""
    with ALERT_STATE["timer_lock"]:
        # Cancel warning timer
        if ALERT_STATE["warning_timer"]:
            ALERT_STATE["warning_timer"].cancel()
            ALERT_STATE["warning_timer"] = None

        # Cancel critical timer
        if ALERT_STATE["critical_timer"]:
            ALERT_STATE["critical_timer"].cancel()
            ALERT_STATE["critical_timer"] = None

        # Cancel all tracked timers
        for timer in ALERT_STATE["active_timers"]:
            if timer.is_alive():
                timer.cancel()
        ALERT_STATE["active_timers"].clear()

        # Reset alert states
        ALERT_STATE["warning_active"] = False
        ALERT_STATE["critical_active"] = False

    app.logger.info("Alert timers cleaned up")

# --- Slack Notification System ---
def slack_worker():
    """
    Background worker thread for sending Slack notifications

    This runs continuously, pulling messages from the queue and sending them.
    This design prevents Slack API calls from blocking the interrupt handler.
    """
    while True:
        try:
            # Block for up to 1 second waiting for a message
            message_data = SLACK_QUEUE.get(timeout=1)

            if message_data is None:  # Shutdown signal
                break

            # Attempt to send with retries
            for attempt in range(3):
                try:
                    _send_slack_notification_internal(**message_data)
                    break
                except Exception as e:
                    if attempt == 2:
                        app.logger.error(f"Failed to send Slack notification after 3 attempts: {e}")
                    else:
                        time.sleep(1)  # Brief delay before retry

        except Empty:
            # No messages in queue, continue waiting
            continue
        except Exception as e:
            app.logger.error(f"Slack worker error: {e}")

def send_slack_notification(message, distance_km=None, energy=None, alert_level=None, previous_level=None):
    """
    Queue a Slack notification for sending with priority handling

    This is the public interface for sending notifications. It adds messages
    to a queue for processing by the background worker thread.

    Args:
        message: Main notification text
        distance_km: Distance to lightning strike in km (optional)
        energy: Energy level of strike (optional)
        alert_level: AlertLevel enum for notification type
        previous_level: Previous AlertLevel for all-clear messages
    """
    if not get_config_boolean('SLACK', 'enabled', False):
        return

    msg_data = {
        'message': message,
        'distance_km': distance_km,
        'energy': energy,
        'alert_level': alert_level,
        'previous_level': previous_level,
        'timestamp': time.time()  # Add timestamp for queue management
    }

    try:
        SLACK_QUEUE.put_nowait(msg_data)
    except:
        # Queue full - handle based on priority
        if alert_level in [AlertLevel.CRITICAL, AlertLevel.WARNING]:
            # For critical messages, force space by removing oldest
            try:
                # Find and remove oldest non-critical message
                temp_queue = []
                removed = False

                while not SLACK_QUEUE.empty():
                    try:
                        item = SLACK_QUEUE.get_nowait()
                        if not removed and item.get('alert_level') not in [AlertLevel.CRITICAL, AlertLevel.WARNING]:
                            removed = True
                            app.logger.warning(f"Removed non-critical message to make space for {alert_level.value}")
                        else:
                            temp_queue.append(item)
                    except Empty:
                        break

                # Put items back
                for item in temp_queue:
                    SLACK_QUEUE.put_nowait(item)

                # Try to add critical message again
                if removed:
                    SLACK_QUEUE.put_nowait(msg_data)
                else:
                    app.logger.error("Failed to queue critical Slack notification - queue full of critical messages")
            except:
                app.logger.error("Failed to manage Slack queue for critical message")
        else:
            app.logger.warning("Slack queue full, dropping non-critical notification")

def _send_slack_notification_internal(message, distance_km=None, energy=None, alert_level=None, previous_level=None, timestamp=None):
    """
    Internal function to actually send Slack notification

    This is called by the worker thread and handles the actual API communication.
    """
    bot_token = CONFIG.get('SLACK', 'bot_token', fallback='')
    channel = CONFIG.get('SLACK', 'channel', fallback='#alerts')

    if not bot_token:
        app.logger.warning("Slack is enabled, but Bot Token is not configured")
        return

    url = 'https://slack.com/api/chat.postMessage'

    # Get unit preference
    use_imperial = get_distance_unit()

    # Determine notification styling based on alert level
    if alert_level == AlertLevel.CRITICAL:
        color, emoji, urgency = "#ff0000", ":rotating_light:", "CRITICAL"
    elif alert_level == AlertLevel.WARNING:
        color, emoji, urgency = "#ff9900", ":warning:", "WARNING"
    elif alert_level == AlertLevel.ALL_CLEAR:
        color, emoji, urgency = "#00ff00", ":white_check_mark:", "ALL CLEAR"
    else:
        color, emoji, urgency = "#ffcc00", ":zap:", "INFO"

    # Build Slack message blocks
    blocks = []

    # Main message block
    if alert_level in [AlertLevel.WARNING, AlertLevel.CRITICAL]:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{emoji} *{urgency} LIGHTNING ALERT* {emoji}\n{message}"
            }
        })

        # Add details if available
        if distance_km is not None and energy is not None:
            distance_str = format_distance(distance_km, use_imperial)
            blocks.append({
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Distance:*\n{distance_str}"},
                    {"type": "mrkdwn", "text": f"*Energy Level:*\n{energy:,}"},
                    {"type": "mrkdwn", "text": f"*Alert Level:*\n{urgency}"},
                    {"type": "mrkdwn", "text": f"*Time:*\n{datetime.now().strftime('%H:%M:%S')}"}
                ]
            })

        # Add context message
        if alert_level == AlertLevel.CRITICAL:
            context_text = ":exclamation: *Very close strike. Take shelter immediately.*"
        else:
            context_text = ":cloud_with_lightning: *Lightning activity in the area. Be prepared.*"

        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": context_text}]
        })

    elif alert_level == AlertLevel.ALL_CLEAR:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{emoji} *{urgency}*\n{message}"}
        })

        # Add context about which zone cleared
        previous_urgency = "WARNING" if previous_level == AlertLevel.WARNING else "CRITICAL"
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f":information_source: No strikes in {previous_urgency.lower()} zone for "
                        f"{get_config_int('ALERTS', 'all_clear_timer', 15)} min."
            }]
        })
    else:
        # Generic message
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{emoji} {message}"}
        })

    # Build payload
    payload = {
        'channel': channel,
        'text': message,  # Fallback text
        'blocks': blocks,
        'icon_emoji': emoji
    }

    # Add color attachment for critical alerts
    if alert_level in [AlertLevel.CRITICAL, AlertLevel.WARNING, AlertLevel.ALL_CLEAR]:
        payload['attachments'] = [{'color': color, 'fallback': message}]

    headers = {
        'Authorization': f'Bearer {bot_token}',
        'Content-Type': 'application/json'
    }

    # Send to Slack API
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()

        result = response.json()
        if not result.get('ok'):
            app.logger.error(f"Slack API error: {result.get('error', 'Unknown error')}")

    except requests.exceptions.Timeout:
        app.logger.warning("Slack notification timed out - continuing operation")
    except requests.exceptions.RequestException as e:
        app.logger.error(f"Slack notification failed: {e}")
    except Exception as e:
        app.logger.error(f"Unexpected error sending Slack notification: {e}")

# --- Dynamic Noise Handling ---
def handle_disturber_event():
    """
    Handle transient disturber events by counting occurrences

    If too many disturbers are detected within a time window, the noise
    floor is raised to reduce sensitivity.
    """
    if not get_config_boolean('NOISE_HANDLING', 'enabled', False):
        return

    now = datetime.now()
    threshold = get_config_int('NOISE_HANDLING', 'event_threshold', 15)
    window = timedelta(seconds=get_config_int('NOISE_HANDLING', 'time_window_seconds', 120))
    revert_delay = get_config_int('NOISE_HANDLING', 'revert_delay_minutes', 10) * 60

    with MONITORING_STATE['lock']:
        # Add this event to the buffer
        MONITORING_STATE['noise_events'].append(now)

        # Remove events outside the time window
        while MONITORING_STATE['noise_events'] and (now - MONITORING_STATE['noise_events'][0]) > window:
            MONITORING_STATE['noise_events'].popleft()

        # Failsafe: prevent buffer from growing too large
        if len(MONITORING_STATE['noise_events']) > 100:
            # Keep only the most recent 50 events
            MONITORING_STATE['noise_events'] = deque(
                list(MONITORING_STATE['noise_events'])[-50:],
                maxlen=50
            )
            app.logger.warning("Noise events buffer exceeded expected size, truncated")

        # Check if threshold exceeded
        if len(MONITORING_STATE['noise_events']) >= threshold and MONITORING_STATE['status']['noise_mode'] != 'Critical':
            # Cancel existing revert timer
            if MONITORING_STATE.get('noise_revert_timer'):
                MONITORING_STATE['noise_revert_timer'].cancel()

            # Raise noise floor if not already raised
            if MONITORING_STATE['status']['noise_mode'] != 'High':
                with SENSOR_INIT_LOCK:
                    if sensor and sensor.is_initialized:
                        app.logger.warning(
                            f"Disturber threshold exceeded ({len(MONITORING_STATE['noise_events'])} events). "
                            f"Elevating noise floor to High."
                        )
                        sensor.set_noise_floor(get_config_int('NOISE_HANDLING', 'raised_noise_floor_level', 5))
                        MONITORING_STATE['status']['noise_mode'] = 'High'

            # Schedule reversion to normal
            timer = threading.Timer(revert_delay, revert_noise_floor, args=['High'])
            timer.daemon = True
            timer.start()
            MONITORING_STATE['noise_revert_timer'] = timer

def handle_noise_high_event():
    """
    Handle persistent noise events (INT_NH interrupt)

    This indicates the noise level is consistently too high, so we
    immediately set the noise floor to maximum.
    """
    # For piezo testing, check if there's also a lightning signature
    distance_km = sensor.get_lightning_distance()
    energy = sensor.get_lightning_energy()

    if distance_km > 0 and distance_km < 0x3F and energy > 0:
        app.logger.warning(f"Noise event has lightning signature! Distance: {distance_km}km, Energy: {energy}")
        # Treat as lightning
        handle_lightning_event()
        return

    if not get_config_boolean('NOISE_HANDLING', 'enabled', False):
        return

    with MONITORING_STATE['lock']:
        # Already at maximum?
        if MONITORING_STATE['status']['noise_mode'] == 'Critical':
            return

        # Cancel any existing timer
        if MONITORING_STATE.get('noise_revert_timer'):
            MONITORING_STATE['noise_revert_timer'].cancel()

        # Set noise floor to maximum
        with SENSOR_INIT_LOCK:
            if sensor and sensor.is_initialized:
                app.logger.critical("Persistent high noise detected (INT_NH). Elevating noise floor to Critical.")
                sensor.set_noise_floor(7)  # Maximum noise floor
                MONITORING_STATE['status']['noise_mode'] = 'Critical'

        # Schedule reversion
        revert_delay = get_config_int('NOISE_HANDLING', 'revert_delay_minutes', 10) * 60
        timer = threading.Timer(revert_delay, revert_noise_floor, args=['Critical'])
        timer.daemon = True
        timer.start()
        MONITORING_STATE['noise_revert_timer'] = timer

def revert_noise_floor(level_to_revert):
    """
    Revert the sensor's noise floor to normal after quiet period

    Args:
        level_to_revert: The noise mode to revert from ('High' or 'Critical')
    """
    with SENSOR_INIT_LOCK:
        if sensor and sensor.is_initialized:
            with MONITORING_STATE['lock']:
                current_mode = MONITORING_STATE['status']['noise_mode']

                # Only revert if we're still in the expected mode
                if current_mode == level_to_revert:
                    app.logger.info(f"Reverting noise floor from {current_mode} to Normal")
                    sensor.set_noise_floor(sensor.original_noise_floor)
                    MONITORING_STATE['status']['noise_mode'] = 'Normal'
                    MONITORING_STATE['noise_events'].clear()

                    # Clear timer reference
                    if MONITORING_STATE.get('noise_revert_timer'):
                        MONITORING_STATE['noise_revert_timer'] = None

# --- Core Monitoring Thread ---
def lightning_monitoring():
    """
    Main monitoring thread with event-driven architecture using gpiozero

    This thread initializes the sensor, sets up GPIO interrupts, and
    monitors sensor health. All actual lightning detection is handled
    via interrupts, making this highly efficient.
    """
    global sensor

    app.logger.info(f"Starting lightning monitoring thread v2.1-Production-Enhanced-gpiozero-Imperial-FIXED (backend: {GPIO_BACKEND})")

    # Initialize sensor with retry logic
    if not initialize_sensor_with_retry():
        app.logger.critical("Failed to initialize sensor after all retries")
        return

    # Setup GPIO interrupt detection with gpiozero
    interrupt_configured = False
    setup_attempts = 0
    max_setup_attempts = 5

    while not interrupt_configured and setup_attempts < max_setup_attempts:
        setup_attempts += 1

        try:
            # Set up the interrupt callback using gpiozero
            sensor.set_interrupt_callback(handle_sensor_interrupt)

            # Verify setup worked
            time.sleep(0.1)
            if sensor.irq_button and (sensor.irq_button.when_pressed or sensor.irq_button.when_released):
                interrupt_configured = True
                app.logger.info(f"gpiozero interrupt configured successfully on attempt {setup_attempts}")
            else:
                raise Exception("Interrupt callback not properly set")

        except Exception as e:
            app.logger.error(f"Failed to setup gpiozero interrupt on attempt {setup_attempts}: {e}")

        if not interrupt_configured and setup_attempts < max_setup_attempts:
            wait_time = min(2 ** (setup_attempts - 1), 10)  # Exponential backoff, max 10s
            app.logger.info(f"Waiting {wait_time}s before retry...")

            # Interruptible wait
            for _ in range(int(wait_time * 10)):
                if MONITORING_STATE['stop_event'].is_set():
                    return
                time.sleep(0.1)

    if not interrupt_configured:
        app.logger.error(f"Failed to setup gpiozero interrupt after {max_setup_attempts} attempts")
        with MONITORING_STATE['lock']:
            MONITORING_STATE['status']['sensor_healthy'] = False
            MONITORING_STATE['status']['last_error'] = "gpiozero interrupt setup failed"
        return

    # Main monitoring loop
    last_health_check = time.time()
    health_check_interval = 300  # 5 minutes
    consecutive_failures = 0
    max_consecutive_failures = 1

    try:
        while not MONITORING_STATE['stop_event'].is_set():
            # Non-blocking wait with frequent checks
            for _ in range(100):  # Check every 0.1s for 10s total
                if MONITORING_STATE['stop_event'].is_set():
                    break
                time.sleep(0.1)

            # Periodic health check
            current_time = time.time()
            if current_time - last_health_check > health_check_interval:
                if not perform_sensor_health_check():
                    consecutive_failures += 1
                    app.logger.warning(f"Sensor health check failed ({consecutive_failures}/{max_consecutive_failures})")

                    # Try to recover after multiple failures
                    if consecutive_failures >= max_consecutive_failures:
                        app.logger.critical(f"Sensor failed {max_consecutive_failures} consecutive health checks")

                        # Remove old interrupt handler
                        if interrupt_configured:
                            try:
                                sensor.remove_interrupt_callback()
                            except:
                                pass

                        # Attempt to reinitialize
                        if initialize_sensor_with_retry(max_retries=3):
                            consecutive_failures = 0

                            # Re-setup interrupt
                            try:
                                sensor.set_interrupt_callback(handle_sensor_interrupt)
                                interrupt_configured = True
                                app.logger.info("Sensor recovered and gpiozero interrupt re-configured")
                            except Exception as e:
                                app.logger.error(f"Failed to re-setup gpiozero interrupt: {e}")
                                break
                        else:
                            app.logger.critical("Failed to recover sensor")
                            break
                else:
                    # Health check passed
                    consecutive_failures = 0

                last_health_check = current_time

    except Exception as e:
        app.logger.error(f"Unexpected error in monitoring loop: {e}", exc_info=True)
        with MONITORING_STATE['lock']:
            MONITORING_STATE['status']['sensor_healthy'] = False
            MONITORING_STATE['status']['last_error'] = str(e)

    finally:
        app.logger.info("Cleaning up monitoring thread (gpiozero)")

        # Remove interrupt detection first
        if interrupt_configured:
            try:
                if sensor:
                    sensor.remove_interrupt_callback()
                app.logger.info("gpiozero interrupt removed")
            except Exception as e:
                app.logger.error(f"Error removing gpiozero interrupt: {e}")

        # Clean up sensor
        with SENSOR_INIT_LOCK:
            if sensor:
                try:
                    sensor.cleanup()
                except Exception as e:
                    app.logger.error(f"Error during sensor cleanup: {e}")
                sensor = None

        # Update status
        with MONITORING_STATE['lock']:
            MONITORING_STATE['status']['sensor_active'] = False
            MONITORING_STATE['status']['status_message'] = "Stopped"

        app.logger.info("Monitoring thread cleanup complete (gpiozero)")

def monitoring_watchdog():
    """
    Watchdog thread that monitors the main monitoring thread

    This provides automatic recovery if the monitoring thread dies
    unexpectedly. It includes failure counting to prevent infinite
    restart loops.
    """
    consecutive_failures = 0
    max_failures = 3

    while not MONITORING_STATE['stop_event'].is_set():
        # Wait 60 seconds between checks
        for _ in range(60):
            if MONITORING_STATE['stop_event'].is_set():
                return
            time.sleep(1)

        with MONITORING_STATE['lock']:
            thread = MONITORING_STATE.get('thread')

            # Check if thread is alive
            if not thread or not thread.is_alive():
                # Only restart if auto-start is enabled
                if not get_config_boolean('SENSOR', 'auto_start', True):
                    continue

                consecutive_failures += 1

                # Give up after too many failures
                if consecutive_failures >= max_failures:
                    app.logger.critical(f"Monitoring thread failed {max_failures} times. Stopping watchdog.")
                    MONITORING_STATE['status']['status_message'] = "Fatal: Too many failures"
                    return

                app.logger.warning(f"Monitoring thread died (failure {consecutive_failures}/{max_failures}). Restarting...")

                # Clear stop event and start new thread
                MONITORING_STATE['stop_event'].clear()
                new_thread = threading.Thread(target=lightning_monitoring, daemon=True)
                MONITORING_STATE['thread'] = new_thread
                new_thread.start()

                # Wait a bit to see if it starts successfully
                time.sleep(5)

                if new_thread.is_alive():
                    consecutive_failures = 0  # Reset on success
                    app.logger.info("Monitoring thread restarted successfully")
            else:
                # Thread is running normally
                consecutive_failures = 0

# --- Flask Web Routes ---
@app.route('/')
def index():
    """Main dashboard page"""
    # Get current state with thread safety
    with MONITORING_STATE['lock']:
        events = list(MONITORING_STATE['events'])
        status = MONITORING_STATE['status'].copy()
        total_events = len(events)

    with ALERT_STATE["timer_lock"]:
        alert_status = {
            'warning_active': ALERT_STATE["warning_active"],
            'critical_active': ALERT_STATE["critical_active"],
            'last_warning_strike': ALERT_STATE["last_warning_strike"].strftime('%H:%M:%S')
                if ALERT_STATE["last_warning_strike"] else None,
            'last_critical_strike': ALERT_STATE["last_critical_strike"].strftime('%H:%M:%S')
                if ALERT_STATE["last_critical_strike"] else None
        }

    # Pre-format event data for template
    for event in events:
        if 'timestamp' in event:
            try:
                event['timestamp'] = datetime.fromisoformat(event['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
            except:
                event['timestamp'] = 'Unknown'

        # Energy is already formatted in the event
        # Distance is already formatted with units in distance_display

    # Format last reading timestamp
    if status.get('last_reading'):
        try:
            status['last_reading'] = datetime.fromisoformat(status['last_reading']).strftime('%Y-%m-%d %H:%M:%S')
        except:
            status['last_reading'] = 'Unknown'

    # Get unit preference for display
    use_imperial = get_distance_unit()
    unit_label = "miles" if use_imperial else "km"

    return render_template('index.html',
        lightning_events=events,
        sensor_status=status,
        alert_state=alert_status,
        config=CONFIG,
        debug_mode=get_config_boolean('SYSTEM', 'debug', False),
        total_event_count=total_events,
        events_truncated=(total_events >= 100),
        use_imperial=use_imperial,
        unit_label=unit_label
    )

@app.route('/api/status')
def api_status():
    """JSON API endpoint for system status"""
    with MONITORING_STATE['lock']:
        status = MONITORING_STATE['status'].copy()
        thread_alive = MONITORING_STATE['thread'].is_alive() if MONITORING_STATE.get('thread') else False
        event_count = len(MONITORING_STATE['events'])

    with ALERT_STATE["timer_lock"]:
        alert_status = {
            'warning_active': ALERT_STATE["warning_active"],
            'critical_active': ALERT_STATE["critical_active"]
        }

    return jsonify({
        **status,
        'alert_state': alert_status,
        'monitoring_thread_active': thread_alive,
        'version': '2.1-Production-Enhanced-gpiozero-Imperial-FIXED',
        'gpio_backend': GPIO_BACKEND,
        'event_count': event_count,
        'config_valid': validate_config(),
        'units': 'imperial' if get_distance_unit() else 'metric'
    })

@app.route('/health')
def health_check():
    """
    Health check endpoint for monitoring system health

    Returns HTTP 200 if healthy, 503 if degraded
    """
    health = {
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'version': '2.1-Production-Enhanced-gpiozero-Imperial-FIXED',
        'gpio_backend': GPIO_BACKEND,
        'units': 'imperial' if get_distance_unit() else 'metric',
        'checks': {}
    }

    # Check sensor
    with SENSOR_INIT_LOCK:
        if sensor and sensor.is_initialized:
            try:
                # Try a register read
                sensor._read_register(0x00)
                health['checks']['sensor'] = 'ok'
            except:
                health['checks']['sensor'] = 'error'
                health['status'] = 'degraded'
        else:
            health['checks']['sensor'] = 'not_initialized'
            health['status'] = 'degraded'

    # Check thread
    with MONITORING_STATE['lock']:
        thread = MONITORING_STATE.get('thread')
        if thread and thread.is_alive():
            health['checks']['monitoring_thread'] = 'running'
        else:
            health['checks']['monitoring_thread'] = 'stopped'
            if get_config_boolean('SENSOR', 'auto_start', True):
                health['status'] = 'degraded'

    # Check gpiozero button
    if sensor and sensor.irq_button:
        try:
            _ = sensor.irq_button.is_pressed
            health['checks']['gpio_button'] = 'ok'
        except:
            health['checks']['gpio_button'] = 'error'
            health['status'] = 'degraded'
    else:
        health['checks']['gpio_button'] = 'not_initialized'

    # Check configuration
    health['checks']['config'] = 'valid' if validate_config() else 'invalid'
    if health['checks']['config'] == 'invalid':
        health['status'] = 'degraded'

    # Check sensor health from status
    with MONITORING_STATE['lock']:
        if not MONITORING_STATE['status'].get('sensor_healthy', True):
            health['checks']['sensor_health'] = 'unhealthy'
            health['status'] = 'degraded'
        else:
            health['checks']['sensor_health'] = 'healthy'

    return jsonify(health), 200 if health['status'] == 'healthy' else 503

@app.route('/metrics')
def metrics():
    """Prometheus-compatible metrics endpoint for external monitoring"""
    with MONITORING_STATE['lock']:
        event_count = len(MONITORING_STATE['events'])
        sensor_active = 1 if MONITORING_STATE['status']['sensor_active'] else 0
        sensor_healthy = 1 if MONITORING_STATE['status']['sensor_healthy'] else 0
        noise_level = {'Normal': 0, 'High': 1, 'Critical': 2}.get(
            MONITORING_STATE['status']['noise_mode'], 0
        )
        interrupt_storm = 1 if MONITORING_STATE['interrupt_storm_detected'] else 0
        use_imperial = 1 if MONITORING_STATE['status'].get('use_imperial', True) else 0

    with ALERT_STATE["timer_lock"]:
        warning_active = 1 if ALERT_STATE["warning_active"] else 0
        critical_active = 1 if ALERT_STATE["critical_active"] else 0
        active_timer_count = len([t for t in ALERT_STATE["active_timers"] if t.is_alive()])

    gpio_backend_metric = 1 if GPIO_BACKEND == "pigpio" else 0

    metrics_text = f"""# HELP lightning_detector_events_total Total lightning events detected
# TYPE lightning_detector_events_total counter
lightning_detector_events_total {event_count}

# HELP lightning_detector_sensor_active Sensor monitoring status (1=active, 0=inactive)
# TYPE lightning_detector_sensor_active gauge
lightning_detector_sensor_active {sensor_active}

# HELP lightning_detector_sensor_healthy Sensor health status (1=healthy, 0=unhealthy)
# TYPE lightning_detector_sensor_healthy gauge
lightning_detector_sensor_healthy {sensor_healthy}

# HELP lightning_detector_noise_level Current noise mitigation level (0=Normal, 1=High, 2=Critical)
# TYPE lightning_detector_noise_level gauge
lightning_detector_noise_level {noise_level}

# HELP lightning_detector_warning_active Warning alert active (1=active, 0=inactive)
# TYPE lightning_detector_warning_active gauge
lightning_detector_warning_active {warning_active}

# HELP lightning_detector_critical_active Critical alert active (1=active, 0=inactive)
# TYPE lightning_detector_critical_active gauge
lightning_detector_critical_active {critical_active}

# HELP lightning_detector_interrupt_storm Interrupt storm detected (1=yes, 0=no)
# TYPE lightning_detector_interrupt_storm gauge
lightning_detector_interrupt_storm {interrupt_storm}

# HELP lightning_detector_active_timers Number of active alert timers
# TYPE lightning_detector_active_timers gauge
lightning_detector_active_timers {active_timer_count}

# HELP lightning_detector_gpio_backend GPIO backend in use (1=pigpio, 0=default)
# TYPE lightning_detector_gpio_backend gauge
lightning_detector_gpio_backend {gpio_backend_metric}

# HELP lightning_detector_use_imperial Units display (1=imperial/miles, 0=metric/km)
# TYPE lightning_detector_use_imperial gauge
lightning_detector_use_imperial {use_imperial}
"""
    return Response(metrics_text, mimetype='text/plain')

@app.route('/config')
def config_page():
    """Configuration page"""
    use_imperial = get_distance_unit()

    # Get config values and format for display
    config_display = {}
    for section in CONFIG.sections():
        config_display[section] = {}
        for key, value in CONFIG.items(section):
            # Format distance values with units
            if 'distance' in key.lower() and section == 'ALERTS':
                try:
                    km_val = int(value)
                    if use_imperial:
                        config_display[section][key] = f"{km_val} km ({format_distance(km_val, True)})"
                    else:
                        config_display[section][key] = f"{km_val} km"
                except:
                    config_display[section][key] = value
            else:
                config_display[section][key] = value

    return render_template('config.html', config=CONFIG, config_display=config_display, use_imperial=use_imperial)

@app.route('/save_config', methods=['POST'])
def save_config_route():
    """Save configuration from web form"""
    try:
        # Define all sections and their checkbox options
        checkbox_options = {
            'SYSTEM': ['debug'],
            'SENSOR': ['indoor', 'auto_start', 'irq_active_high'],
            'NOISE_HANDLING': ['enabled'],
            'SLACK': ['enabled'],
            'DISPLAY': ['use_imperial_units']
        }

        # First, set all known checkbox options to 'false'
        for section, options in checkbox_options.items():
            if not CONFIG.has_section(section):
                CONFIG.add_section(section)
            for option in options:
                CONFIG.set(section, option, 'false')

        # Now, update all values from the submitted form
        for key, value in request.form.items():
            if '_' in key:
                section, option = key.split('_', 1)
                if not CONFIG.has_section(section):
                    CONFIG.add_section(section)

                # If the value is 'true' it's a checkbox, otherwise it's a text input
                CONFIG.set(section, option, value)

        # Save to file
        with open('config.ini', 'w') as configfile:
            CONFIG.write(configfile)

        # Update the global status with new unit preference
        with MONITORING_STATE['lock']:
            MONITORING_STATE['status']['use_imperial'] = get_distance_unit()

        flash('Configuration saved successfully! A restart may be needed to apply all changes.', 'success')
        app.logger.info("Configuration updated via web interface")

    except Exception as e:
        flash(f'Error saving configuration: {str(e)}', 'error')
        app.logger.error(f"Configuration save error: {e}")

    return redirect(url_for('config_page'))

@app.route('/start_monitoring')
def start_monitoring_route():
    """Start the monitoring thread"""
    with MONITORING_STATE['lock']:
        if MONITORING_STATE.get('thread') and MONITORING_STATE['thread'].is_alive():
            flash('Monitoring is already running', 'warning')
        else:
            # Clear stop event and start new thread
            MONITORING_STATE['stop_event'].clear()
            thread = threading.Thread(target=lightning_monitoring, daemon=True)
            MONITORING_STATE['thread'] = thread
            thread.start()
            flash(f'Monitoring started successfully (gpiozero backend: {GPIO_BACKEND})', 'success')
            app.logger.info("Monitoring started via web interface (gpiozero)")

    return redirect(url_for('index'))

@app.route('/stop_monitoring')
def stop_monitoring_route():
    """Stop the monitoring thread"""
    with MONITORING_STATE['lock']:
        if not MONITORING_STATE.get('thread') or not MONITORING_STATE['thread'].is_alive():
            flash('Monitoring is not running', 'warning')
        else:
            # Signal thread to stop
            MONITORING_STATE['stop_event'].set()
            flash('Monitoring stop requested. Please wait...', 'info')
            app.logger.info("Monitoring stop requested via web interface (gpiozero)")

    # Clean up alert timers
    cleanup_alert_timers()

    return redirect(url_for('index'))

@app.route('/test_alerts')
def test_alerts():
    """Test alert functionality (debug mode only)"""
    if not get_config_boolean('SYSTEM', 'debug', False):
        flash('Test alerts only available in debug mode', 'error')
        return redirect(url_for('index'))

    alert_type = request.args.get('type', 'warning')
    use_imperial = get_distance_unit()

    # Create fake event
    if alert_type == 'critical':
        distance_km = 5
        alert_level = AlertLevel.CRITICAL
        message = f"🚨 TEST CRITICAL ALERT: This is a test of the critical alert system (gpiozero, {format_distance(distance_km, use_imperial)})"
    else:
        distance_km = 20
        alert_level = AlertLevel.WARNING
        message = f"⚠️ TEST WARNING ALERT: This is a test of the warning alert system (gpiozero, {format_distance(distance_km, use_imperial)})"

    # Send test notification
    send_slack_notification(message, distance_km, 999999, alert_level)

    # Update alert state for UI
    with ALERT_STATE["timer_lock"]:
        if alert_level == AlertLevel.CRITICAL:
            ALERT_STATE["critical_active"] = True
            ALERT_STATE["last_critical_strike"] = datetime.now()
        else:
            ALERT_STATE["warning_active"] = True
            ALERT_STATE["last_warning_strike"] = datetime.now()

    flash(f'Test {alert_type} alert sent (gpiozero backend: {GPIO_BACKEND})', 'success')
    return redirect(url_for('index'))

@app.route('/reset_alerts')
def reset_alerts():
    """Reset all active alerts"""
    cleanup_alert_timers()

    # Clear alert state
    with ALERT_STATE["timer_lock"]:
        ALERT_STATE["warning_active"] = False
        ALERT_STATE["critical_active"] = False
        ALERT_STATE["last_warning_strike"] = None
        ALERT_STATE["last_critical_strike"] = None

    flash('All alerts have been reset', 'success')
    app.logger.info("Alerts reset via web interface (gpiozero)")

    return redirect(url_for('index'))

@app.route('/test_slack')
def test_slack():
    """Test Slack integration"""
    if not get_config_boolean('SLACK', 'enabled', False):
        flash('Slack notifications are disabled', 'warning')
        return redirect(url_for('config_page'))

    use_imperial = get_distance_unit()
    units = "Imperial (miles)" if use_imperial else "Metric (km)"
    send_slack_notification(
        f"🧪 Test message from Lightning Detector v2.1 (gpiozero backend: {GPIO_BACKEND}, units: {units})",
        alert_level=AlertLevel.WARNING
    )

    flash('Test message sent to Slack. Check your Slack channel.', 'info')
    return redirect(url_for('config_page'))

@app.route('/check_sensor')
def check_sensor():
    """Check and fix sensor configuration"""
    if not sensor:
        return jsonify({"error": "Sensor not initialized"}), 500

    results = {"timestamp": datetime.now().isoformat()}

    try:
        # Read all important registers
        regs = {}
        for addr in [0x00, 0x01, 0x02, 0x03, 0x07]:
            val = sensor._read_register(addr)
            regs[f"0x{addr:02X}"] = f"0x{val:02X}"

        # Check and fix MASK_DIST if needed
        reg03 = sensor._read_register(0x03)
        mask_dist = (reg03 >> 5) & 0x01

        if mask_dist:
            # Fix it!
            app.logger.warning("MASK_DIST was set - fixing now!")
            new_reg03 = reg03 & ~(1 << 5)
            sensor._write_register(0x03, new_reg03)
            time.sleep(0.002)

            # Verify fix
            reg03_after = sensor._read_register(0x03)
            results["mask_dist_fixed"] = True
            results["reg03_before"] = f"0x{reg03:02X}"
            results["reg03_after"] = f"0x{reg03_after:02X}"
        else:
            results["mask_dist_fixed"] = False
            results["mask_dist_ok"] = True

        # Clear any pending interrupts
        for _ in range(5):
            _ = sensor._read_register(0x03)
            time.sleep(0.001)

        results["registers"] = regs
        results["status"] = "Sensor checked and fixed if needed"

        return jsonify(results)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/force_recalibrate')
def force_recalibrate():
    """
    Perform an aggressive reset and recalibration of the sensor.
    This is useful for clearing persistent noise flags (INT_NH).
    """
    if not sensor:
        return jsonify({"error": "Sensor not initialized"}), 500

    app.logger.warning("=== FORCING SENSOR RECALIBRATION ===")
    results = {"steps": []}

    try:
        with SENSOR_INIT_LOCK:
            # Step 1: Force a direct reset command
            sensor._write_register(0x3C, 0x96)
            time.sleep(0.005)
            results["steps"].append("Sent direct reset command (0x3C = 0x96)")

            # Step 2: Temporarily set to a less sensitive indoor setting
            # AFE_GB=01110 (Outdoor setting)
            sensor._write_register(0x00, 0b00011100)
            time.sleep(0.002)
            results["steps"].append("Set AFE gain to outdoor mode temporarily")

            # Step 3: Set noise floor to a high level to force it to settle
            sensor._write_register(0x01, (0x07 << 4) | 0x0F) # Max noise floor, max watchdog
            time.sleep(0.002)
            results["steps"].append("Set noise floor to maximum temporarily")

            # Step 4: Aggressively clear any pending interrupts
            for i in range(10):
                _ = sensor._read_register(0x03)
                time.sleep(0.002)
            results["steps"].append("Aggressively cleared interrupt register 10 times")

            # Step 5: Restore the high-sensitivity indoor settings from power_up()
            sensor.power_up()
            results["steps"].append("Restored original high-sensitivity indoor settings via power_up()")

            # Step 6: Final check of the interrupt register
            final_int = sensor._read_register(0x03) & 0x0F
            results["final_interrupt_value"] = f"0x{final_int:02X}"
            if final_int == 0x00:
                results["status"] = "SUCCESS: Sensor recalibrated and idle."
                app.logger.info("Force recalibration successful. Sensor is idle (INT=0x00).")
            else:
                results["status"] = "FAILURE: Sensor still has a pending interrupt."
                app.logger.error(f"Force recalibration failed. INT is still 0x{final_int:02X}.")

        return jsonify(results)

    except Exception as e:
        app.logger.error(f"Error during force recalibration: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

# --- Application Initialization ---
def load_config():
    """Load configuration from file"""
    config_file = 'config.ini'
    if os.path.exists(config_file):
        CONFIG.read(config_file)
        app.logger.info(f"Configuration loaded from {config_file}")

        # Ensure DISPLAY section exists with default values
        if not CONFIG.has_section('DISPLAY'):
            CONFIG.add_section('DISPLAY')
            CONFIG.set('DISPLAY', 'use_imperial_units', 'true')
            # Save updated config
            with open(config_file, 'w') as configfile:
                CONFIG.write(configfile)
            app.logger.info("Added DISPLAY section to config with imperial units as default")

        # Validate configuration
        if not validate_config():
            app.logger.warning("Configuration validation failed - check logs for details")
    else:
        app.logger.error(f"Configuration file {config_file} not found!")
        raise FileNotFoundError(f"Configuration file {config_file} not found!")

def initialize_logging():
    """Configure application logging"""
    log_level = CONFIG.get('LOGGING', 'level', fallback='INFO')
    max_size = get_config_int('LOGGING', 'max_file_size', 10) * 1024 * 1024
    backup_count = get_config_int('LOGGING', 'backup_count', 5)

    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # File handler with rotation
    file_handler = RotatingFileHandler(
        'lightning_detector.log',
        maxBytes=max_size,
        backupCount=backup_count
    )
    file_handler.setFormatter(formatter)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    # Configure Flask logger
    app.logger.setLevel(getattr(logging, log_level))
    app.logger.addHandler(file_handler)
    app.logger.addHandler(console_handler)

    # Add rate limiting filter
    rate_filter = RateLimitFilter()
    app.logger.addFilter(rate_filter)

    # Reduce Werkzeug logging
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    app.logger.info(f"Logging initialized (gpiozero backend: {GPIO_BACKEND})")

def cleanup_resources():
    """Cleanup function called on application shutdown"""
    app.logger.info(f"Starting application cleanup (gpiozero backend: {GPIO_BACKEND})...")

    # Stop monitoring
    with MONITORING_STATE['lock']:
        MONITORING_STATE['stop_event'].set()

    # Stop Slack worker
    if SLACK_WORKER_THREAD and SLACK_WORKER_THREAD.is_alive():
        SLACK_QUEUE.put(None)  # Shutdown signal
        SLACK_WORKER_THREAD.join(timeout=5)

    # Wait for monitoring thread to stop
    with MONITORING_STATE['lock']:
        thread = MONITORING_STATE.get('thread')
        if thread and thread.is_alive():
            thread.join(timeout=10)

    # Wait for watchdog to stop
    with MONITORING_STATE['lock']:
        watchdog = MONITORING_STATE.get('watchdog_thread')
        if watchdog and watchdog.is_alive():
            watchdog.join(timeout=5)

    # Clean up alert timers
    cleanup_alert_timers()

    # Clean up sensor (gpiozero cleanup)
    global sensor
    if sensor:
        try:
            sensor.cleanup()
        except:
            pass

    app.logger.info(f"Application cleanup complete (gpiozero backend: {GPIO_BACKEND})")

# Register cleanup function
atexit.register(cleanup_resources)

# --- Interrupt Handler (defined before starting monitoring) ---
def handle_sensor_interrupt(channel):
    """
    GPIO interrupt callback (gpiozero adaptation):
    Determines interrupt source, dispatches handlers, and clears the interrupt.
    """
    try:
        # 1. Read the reason for the interrupt
        reason = sensor.get_interrupt_reason()

        # 2. Dispatch the appropriate handler
        if reason & AS3935LightningDetector.INT_L:
            handle_lightning_event()
        elif reason & AS3935LightningDetector.INT_D:
            handle_disturber_event()
        elif reason & AS3935LightningDetector.INT_NH:
            handle_noise_high_event()
        else:
            # This can happen if the interrupt clears before we read it
            app.logger.debug(f"Spurious interrupt or already cleared. Reason: 0x{reason:02X}")

        # 3. Update status
        with MONITORING_STATE['lock']:
            MONITORING_STATE['status']['last_reading'] = datetime.now().isoformat()

    except Exception as e:
        app.logger.error(f"Interrupt handler error: {e}", exc_info=True)

    finally:
        # 4. CRITICAL FIX: Ensure the interrupt is cleared by reading the register again.
        # This allows the IRQ pin to return to its idle state for the next event.
        # A small delay is added as per best practices for this sensor.
        time.sleep(0.002) # 2ms delay
        if sensor:
            _ = sensor.get_interrupt_reason()

# --- Main Application Entry Point ---
if __name__ == '__main__':
    try:
        # Initialize logging first
        initialize_logging()

        app.logger.info("="*60)
        app.logger.info(f"Lightning Detector v2.1-Production-Enhanced-gpiozero-Imperial-FIXED Starting")
        app.logger.info(f"GPIO Backend: {GPIO_BACKEND}")
        app.logger.info("="*60)

        # Load configuration
        load_config()

        # Start Slack worker thread
        SLACK_WORKER_THREAD = threading.Thread(target=slack_worker, daemon=True)
        SLACK_WORKER_THREAD.start()
        app.logger.info("Slack notification worker started")

        # Auto-start monitoring if configured
        if get_config_boolean('SENSOR', 'auto_start', True):
            app.logger.info(f"Auto-starting monitoring (gpiozero backend: {GPIO_BACKEND})...")
            with MONITORING_STATE['lock']:
                MONITORING_STATE['stop_event'].clear()
                thread = threading.Thread(target=lightning_monitoring, daemon=True)
                MONITORING_STATE['thread'] = thread
                thread.start()

            # Start watchdog
            time.sleep(2)  # Give monitoring thread time to start
            watchdog = threading.Thread(target=monitoring_watchdog, daemon=True)
            MONITORING_STATE['watchdog_thread'] = watchdog
            watchdog.start()
            app.logger.info("Watchdog thread started")

        # Determine host and port
        debug_mode = get_config_boolean('SYSTEM', 'debug', False)
        host = '0.0.0.0'  # Listen on all interfaces
        port = 5000

        # Display units being used
        use_imperial = get_distance_unit()
        units_msg = "Using IMPERIAL units (miles)" if use_imperial else "Using METRIC units (km)"
        app.logger.info(units_msg)

        app.logger.info(f"Starting web server on {host}:{port} (debug={debug_mode}, gpio_backend={GPIO_BACKEND})")

        # Run Flask app
        app.run(host=host, port=port, debug=debug_mode, threaded=True)

    except KeyboardInterrupt:
        app.logger.info("Keyboard interrupt received")
    except Exception as e:
        if 'app' in locals():
            app.logger.critical(f"Fatal error: {e}", exc_info=True)
        else:
            print(f"Fatal error: {e}")
    finally:
        cleanup_resources()
