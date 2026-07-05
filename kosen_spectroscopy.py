#!/usr/bin/env python3
"""
KOSEN USB Spectroscopy
Fedele replica dell'interfaccia e logica originale VB.NET

Replicates the original Theremino Spectrometer with full UI and functionality
"""

import sys
import cv2
import numpy as np
import json
from pathlib import Path
from datetime import datetime
from enum import IntEnum
from dataclasses import dataclass
from typing import Optional, Tuple, List
import pickle
import configparser
from scipy.signal import find_peaks

import smile_correction

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QComboBox, QSpinBox, QCheckBox, QDoubleSpinBox,
    QGroupBox, QFormLayout, QGridLayout, QTextEdit, QFileDialog, QLineEdit,
    QMenuBar, QMenu, QMessageBox, QToolBar, QStatusBar, QScrollArea
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread, QSize, QRect, QEvent
from PyQt6.QtGui import QImage, QPixmap, QFont, QColor, QAction, QIcon, QPainter, QPen
import pyqtgraph as pg
import pyqtgraph.exporters  # noqa: F401 (needed so pg.exporters is available)


# ============================================================================
# CORE SPECTROMETER LOGIC
# ============================================================================

class SensorTypes(IntEnum):
    WEBCAM = 0
    TCD1304 = 1
    TCD1254 = 2


@dataclass
class SpectrometerConfig:
    sensor_type: SensorTypes = SensorTypes.WEBCAM
    sensor_num_samples: int = 3600

    # Requested camera capture resolution (0 = request the sensor's maximum).
    # Higher width = more spectral samples = higher spectral resolution.
    cam_width: int = 0
    cam_height: int = 0

    # ROI
    start_x: int = 0
    end_x: int = 1000
    start_y: int = 500
    size_y: int = 100

    # Filtering
    average_filter_alpha: float = 0.1
    rising_speed: float = 0.1
    falling_speed: float = 0.1
    spatial_averaging: int = 5

    # Display
    flip_h: bool = False
    flip_v: bool = False
    show_dips: bool = True
    show_peaks: bool = True
    use_colors: bool = True
    trim_scale: bool = False
    log_scale: float = 1.0

    # ADC
    adc_min: int = 0
    adc_max: int = 65535
    adc_auto_min: bool = True


class ThereminoSpectrometer:
    """Core spectrometer - replicates original VB.NET logic"""

    def __init__(self, config: SpectrometerConfig = None):
        self.config = config or SpectrometerConfig()
        self.camera: Optional[cv2.VideoCapture] = None
        self.current_frame: Optional[np.ndarray] = None

        # Arrays (like original Module_Spectrometer.vb)
        self.array_received_samples = np.zeros(self.config.sensor_num_samples, dtype=np.float32)
        self.array_average_filtered = np.zeros(self.config.sensor_num_samples, dtype=np.float32)
        self.array_updown_filtered = np.zeros(self.config.sensor_num_samples, dtype=np.float32)
        self.array_calibrated = np.zeros(self.config.sensor_num_samples, dtype=np.float32)
        self.array_spatial_filtered = np.zeros(self.config.sensor_num_samples, dtype=np.float32)
        self.array_visible_samples = np.zeros(self.config.sensor_num_samples, dtype=np.float32)

        # Source image dimensions
        self.src_w = 1600
        self.src_h = 1
        self.roi_x0 = 0  # Absolute left column of the current ROI (frame pixels)

        # Smile (spectral line curvature) correction
        self.smile = None            # smile_correction.SmileCorrection or None
        self.smile_enabled = False   # apply to live frames when True

        # Calibration (like Module_Calibrations.vb)
        self.calib_bin = np.array([1000.0, 2000.0])  # Pixel positions
        self.calib_nm = np.array([436.0, 546.0])     # Wavelengths in nm

        # Peak detection
        self.peaks = []

        # File data
        self.last_saved_file = ""

    def connect_webcam(self, device_id: int = 0) -> bool:
        """Connect to WebCam at the highest useful resolution.

        Spectral resolution == number of horizontal pixels the spectrum spans,
        so we avoid downgrading the sensor. Many USB webcams only expose their
        high resolutions through the MJPG stream, so we request MJPG first.
        """
        try:
            self.camera = cv2.VideoCapture(device_id)
            if not self.camera.isOpened():
                return False

            # MJPG unlocks high-res modes on most USB webcams
            try:
                self.camera.set(cv2.CAP_PROP_FOURCC,
                                cv2.VideoWriter_fourcc(*"MJPG"))
            except Exception:
                pass

            # Request either the user-chosen size or the sensor maximum.
            if self.config.cam_width > 0 and self.config.cam_height > 0:
                req_w, req_h = self.config.cam_width, self.config.cam_height
            else:
                # Ask for something huge; OpenCV/driver clamps to the real max.
                req_w, req_h = 4096, 2160
            self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, req_w)
            self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, req_h)
            self.camera.set(cv2.CAP_PROP_FPS, 30)

            # Use the resolution the camera actually accepted
            self.src_w = int(self.camera.get(cv2.CAP_PROP_FRAME_WIDTH))
            self.src_h = int(self.camera.get(cv2.CAP_PROP_FRAME_HEIGHT))
            if self.src_w <= 0 or self.src_h <= 0:
                # Fallback: read one frame to learn the real size
                ok, fr = self.camera.read()
                if ok and fr is not None:
                    self.src_h, self.src_w = fr.shape[:2]

            if self.config.sensor_type == SensorTypes.WEBCAM:
                self.config.sensor_num_samples = self.src_w
                self._init_arrays()

            return True
        except Exception as e:
            print(f"Camera error: {e}")
            return False

    def disconnect_webcam(self):
        """Disconnect from camera"""
        if self.camera:
            self.camera.release()
            self.camera = None

    def read_frame(self) -> bool:
        """Read frame from camera - returns True if successful"""
        if not self.camera:
            return False
        ret, frame = self.camera.read()
        if ret:
            # Apply flip to frame BEFORE processing (optical flip)
            if self.config.flip_h:
                frame = cv2.flip(frame, 1)  # Flip horizontally
            if self.config.flip_v:
                frame = cv2.flip(frame, 0)  # Flip vertically

            # Smile correction: straighten curved spectral lines so the vertical
            # ROI average stays sharp. Precomputed map -> single fast remap.
            if self.smile_enabled and self.smile is not None and self.smile.matches(frame):
                frame = self.smile.apply(frame)

            self.current_frame = frame
            return True
        return False

    def extract_spectrum(self) -> np.ndarray:
        """Extract spectral line from frame (replicates Form1.vb logic)"""
        if self.current_frame is None:
            return np.zeros(self.config.sensor_num_samples, dtype=np.float32)

        frame = self.current_frame
        height, width = frame.shape[:2]

        # Validate dimensions
        if height <= 0 or width <= 0:
            return np.zeros(self.config.sensor_num_samples, dtype=np.float32)

        # Calculate ROI (from Module_Spectrometer.vb Spectrometer_SetSourceParams)
        src_x0 = max(0, (width * self.config.start_x) // 1000)
        src_dx = width - src_x0 + (width * (self.config.end_x - 1000)) // 1000
        if src_x0 + src_dx > width:
            src_dx = width - src_x0
        src_dx = max(1, src_dx)

        src_y0 = height - (height * self.config.start_y // 1000) - ((height * self.config.size_y) // 1000)
        src_dy = (height * self.config.size_y) // 1000
        if src_y0 + src_dy > height:
            src_dy = height - src_y0
        src_dy = max(1, src_dy)
        src_y0 = max(0, src_y0)

        # Extract ROI safely
        try:
            roi = frame[src_y0:min(height, src_y0+src_dy), src_x0:min(width, src_x0+src_dx)]

            if roi.shape[0] <= 0 or roi.shape[1] <= 0:
                return np.zeros(self.config.sensor_num_samples, dtype=np.float32)

            # Convert to spectrum (ITU-R BT.709 weights)
            spectrum = np.mean(roi, axis=0)  # Average over Y axis

            # Handle grayscale or RGB
            if len(spectrum.shape) > 1 and spectrum.shape[1] >= 3:
                b = spectrum[:, 0] * 0.114
                g = spectrum[:, 1] * 0.587
                r = spectrum[:, 2] * 0.299
                spectrum = (r + g + b).astype(np.float32)
            else:
                spectrum = spectrum.astype(np.float32)

            # Normalize
            if len(spectrum) > 0 and spectrum.max() > 0:
                spectrum = spectrum / 255.0

            # Keep the natural ROI width (NO padding). This makes X Start / X End
            # actually crop the plotted curve. The absolute left column is stored
            # so the plot x-axis can represent true pixel/wavelength positions.
            self.roi_x0 = src_x0
        except Exception as e:
            print(f"Extract spectrum error: {e}")
            return np.zeros(self.config.sensor_num_samples, dtype=np.float32)

        self.array_received_samples = spectrum
        return spectrum

    def apply_filters(self) -> np.ndarray:
        """Apply IIR and spatial filters"""
        # Average filter
        n = len(self.array_received_samples)
        alpha = self.config.average_filter_alpha
        self.array_average_filtered = np.zeros(n, dtype=np.float32)

        prev = self.array_received_samples[0]
        for i in range(n):
            curr = alpha * self.array_received_samples[i] + (1 - alpha) * prev
            self.array_average_filtered[i] = curr
            prev = curr

        # Spatial filter (Savitzky-Golay)
        from scipy.signal import savgol_filter
        window = self.config.spatial_averaging
        if window % 2 == 0:
            window += 1
        if window > len(self.array_average_filtered):
            window = len(self.array_average_filtered) - 1 if len(self.array_average_filtered) > 1 else 1

        if window >= 3:
            self.array_spatial_filtered = savgol_filter(self.array_average_filtered, window, 2)
        else:
            self.array_spatial_filtered = self.array_average_filtered.copy()

        self.array_visible_samples = self.array_spatial_filtered
        return self.array_visible_samples

    def _init_arrays(self):
        """Initialize arrays"""
        n = self.config.sensor_num_samples
        self.array_received_samples = np.zeros(n, dtype=np.float32)
        self.array_average_filtered = np.zeros(n, dtype=np.float32)
        self.array_updown_filtered = np.zeros(n, dtype=np.float32)
        self.array_calibrated = np.zeros(n, dtype=np.float32)
        self.array_spatial_filtered = np.zeros(n, dtype=np.float32)
        self.array_visible_samples = np.zeros(n, dtype=np.float32)

    # ========================================================================
    # CALIBRATION METHODS (replicate Module_Calibrations.vb)
    # ========================================================================

    def interpolate(self, x: np.ndarray, y: np.ndarray, xvalue: float) -> float:
        """Linear interpolation (replicates Module_Calibrations.vb)"""
        i = 1
        while i < len(x) and x[i] < xvalue:
            i += 1
        if i >= len(x):
            i = len(x) - 1

        x0, x1 = x[i-1], x[i]
        y0, y1 = y[i-1], y[i]

        if x1 == x0:
            return float(y0)

        return float(y0 + (xvalue - x0) / (x1 - x0) * (y1 - y0))

    def bin_to_nm(self, bin_val: float) -> float:
        """Convert bin (pixel) to nanometers"""
        return self.interpolate(self.calib_bin, self.calib_nm, bin_val)

    def nm_to_bin(self, nm: float) -> float:
        """Convert nanometers to bin (pixel)"""
        return self.interpolate(self.calib_nm, self.calib_bin, nm)

    def get_wavelengths(self) -> np.ndarray:
        """Get wavelength array from calibration"""
        pixels = np.arange(self.config.sensor_num_samples)
        wavelengths = np.array([self.bin_to_nm(p) for p in pixels])
        return wavelengths

    def save_calibration(self, filepath: str) -> bool:
        """Save calibration to INI file"""
        try:
            config = configparser.ConfigParser()
            config['Calibration'] = {
                'BIN': '|'.join(map(str, self.calib_bin)),
                'NM': '|'.join(map(str, self.calib_nm))
            }
            with open(filepath, 'w') as f:
                config.write(f)
            return True
        except Exception as e:
            print(f"Failed to save calibration: {e}")
            return False

    def load_calibration(self, filepath: str) -> bool:
        """Load calibration from INI file"""
        try:
            config = configparser.ConfigParser()
            config.read(filepath)

            if 'Calibration' not in config:
                return False

            bin_str = config['Calibration'].get('BIN', '')
            nm_str = config['Calibration'].get('NM', '')

            self.calib_bin = np.array([float(x) for x in bin_str.split('|')])
            self.calib_nm = np.array([float(x) for x in nm_str.split('|')])

            return True
        except Exception as e:
            print(f"Failed to load calibration: {e}")
            return False

    # ========================================================================
    # PEAK DETECTION
    # ========================================================================

    def find_peaks_in_spectrum(self, min_height: float = 0.1) -> List[dict]:
        """Find peaks in current spectrum"""
        spectrum = self.array_visible_samples

        # Normalize
        if spectrum.max() <= 0:
            return []

        spectrum_norm = spectrum / spectrum.max()

        # Find peaks
        peaks, properties = find_peaks(spectrum_norm, height=min_height, distance=5)

        self.peaks = []
        wavelengths = self.get_wavelengths()

        for idx in peaks:
            if idx < len(wavelengths):
                peak_data = {
                    'pixel': int(idx),
                    'wavelength': float(wavelengths[idx]),
                    'intensity': float(spectrum[idx]),
                    'height': float(spectrum_norm[idx])
                }
                self.peaks.append(peak_data)

        return self.peaks

    # ========================================================================
    # FILE I/O
    # ========================================================================

    def save_spectrum_csv(self, filepath: str) -> bool:
        """Save current spectrum to CSV file"""
        try:
            wavelengths = self.get_wavelengths()

            with open(filepath, 'w') as f:
                f.write("# Theremino Spectrometer Data\n")
                f.write(f"# Timestamp: {datetime.now().isoformat()}\n")
                f.write(f"# Wavelength (nm), Intensity\n")

                for wl, intensity in zip(wavelengths, self.array_visible_samples):
                    f.write(f"{wl:.2f}, {intensity:.6f}\n")

            self.last_saved_file = filepath
            return True
        except Exception as e:
            print(f"Failed to save spectrum: {e}")
            return False

    def load_data_file(self, filepath: str) -> bool:
        """Load data file"""
        try:
            data = np.loadtxt(filepath, delimiter=',', comments='#')
            if data.ndim == 1:
                # Single spectrum
                self.array_visible_samples = data.astype(np.float32)
            else:
                # Multiple columns - use second column
                self.array_visible_samples = data[:, 1].astype(np.float32)

            self.last_saved_file = filepath
            return True
        except Exception as e:
            print(f"Failed to load data file: {e}")
            return False


class InteractivePreviewLabel(QLabel):
    """Interactive preview: crosshair cursor + click-to-set ROI.

    The pixmap is drawn at NATIVE size and CENTERED inside the label
    (setScaledContents=False + AlignCenter). All coordinate conversions
    account for both the centering offset AND the frame->pixmap scale,
    using a single consistent model.
    """
    pixel_clicked = pyqtSignal(int, int)  # frame coords
    mouse_moved = pyqtSignal(int, int)    # frame coords

    def __init__(self):
        super().__init__()
        self.setMouseTracking(True)
        self.cursor_x = 0          # ORIGINAL frame coords
        self.cursor_y = 0
        self.cursor_inside = False
        self.orig_w = 0            # original frame size
        self.orig_h = 0
        self.disp_w = 0            # displayed pixmap size (native)
        self.disp_h = 0

    def set_frame_pixmap(self, pixmap, orig_w, orig_h):
        """Set displayed pixmap and remember the original frame size."""
        self.orig_w = orig_w
        self.orig_h = orig_h
        self.disp_w = pixmap.width()
        self.disp_h = pixmap.height()
        self.setPixmap(pixmap)

    def _offsets(self):
        """Top-left offset of the centered pixmap within the label."""
        ox = (self.width() - self.disp_w) / 2.0
        oy = (self.height() - self.disp_h) / 2.0
        return ox, oy

    def _label_to_frame(self, lx, ly):
        """label px -> (frame_x, frame_y, inside)."""
        if self.disp_w <= 0 or self.disp_h <= 0:
            return 0, 0, False
        ox, oy = self._offsets()
        pmx = lx - ox   # position inside the pixmap
        pmy = ly - oy
        inside = (0 <= pmx < self.disp_w) and (0 <= pmy < self.disp_h)
        fx = int(pmx * self.orig_w / self.disp_w)
        fy = int(pmy * self.orig_h / self.disp_h)
        fx = max(0, min(self.orig_w - 1, fx))
        fy = max(0, min(self.orig_h - 1, fy))
        return fx, fy, inside

    def _frame_to_label(self, fx, fy):
        """frame px -> label px."""
        ox, oy = self._offsets()
        lx = ox + fx * self.disp_w / self.orig_w
        ly = oy + fy * self.disp_h / self.orig_h
        return lx, ly

    def mouseMoveEvent(self, event):
        fx, fy, inside = self._label_to_frame(event.pos().x(), event.pos().y())
        self.cursor_x, self.cursor_y, self.cursor_inside = fx, fy, inside
        self.mouse_moved.emit(fx, fy)
        self.update()

    def leaveEvent(self, event):
        """Hide the crosshair when the mouse leaves the preview."""
        self.cursor_inside = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        fx, fy, inside = self._label_to_frame(event.pos().x(), event.pos().y())
        if inside:
            self.pixel_clicked.emit(fx, fy)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.disp_w <= 0 or self.orig_w <= 0 or not self.cursor_inside:
            return

        painter = QPainter(self)
        ox, oy = self._offsets()
        lx, ly = self._frame_to_label(self.cursor_x, self.cursor_y)

        pen = QPen(QColor(255, 0, 0), 1, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawLine(int(lx), int(oy), int(lx), int(oy + self.disp_h))
        painter.drawLine(int(ox), int(ly), int(ox + self.disp_w), int(ly))

        info = f"px {self.cursor_x} x {self.cursor_y}"
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(info)
        painter.fillRect(int(ox) + 4, int(oy) + 4, tw + 10, fm.height() + 4,
                         QColor(0, 0, 0, 160))
        painter.setPen(QPen(QColor(255, 255, 0)))
        painter.drawText(int(ox) + 9, int(oy) + 4 + fm.ascent() + 2, info)


class AcquisitionThread(QThread):
    """Background acquisition thread"""
    spectrum_ready = pyqtSignal(object)  # (x_array, y_array)
    preview_ready = pyqtSignal(object)   # preview image

    def __init__(self, spectrometer: ThereminoSpectrometer):
        super().__init__()
        self.spectrometer = spectrometer
        self.running = False

    def run(self):
        self.running = True
        while self.running:
            if self.spectrometer.read_frame():
                spectrum = self.spectrometer.extract_spectrum()
                filtered = self.spectrometer.apply_filters()

                x = np.arange(len(filtered))
                self.spectrum_ready.emit((x, filtered))

                # Emit preview
                frame = self.spectrometer.current_frame
                if frame is not None:
                    self.preview_ready.emit(frame)

            self.msleep(100)

    def stop(self):
        self.running = False
        self.wait()


# ============================================================================
# MAIN GUI WINDOW - Replica dell'originale
# ============================================================================

class ThereminoSpectrometryGUI(QMainWindow):
    """Main GUI window - Theremino Spectrometer V5.0 replica"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("KOSEN USB Spectroscopy")
        # Set window icon
        icon_path = Path(__file__).parent / "icon.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        # Make window responsive - 85% of screen (fall back to a fixed size
        # if no screen is reported, e.g. some headless/remote setups)
        _pscreen = QApplication.primaryScreen()
        if _pscreen is not None:
            screen = _pscreen.geometry()
            self.setGeometry(
                int(screen.width() * 0.075),
                int(screen.height() * 0.05),
                int(screen.width() * 0.85),
                int(screen.height() * 0.9)
            )
        else:
            self.resize(1280, 800)

        # Theme colors
        self.setStyleSheet("""
            QMainWindow { background-color: #f5f5f5; }
            QGroupBox {
                border: 2px solid #0066cc;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
                color: #333;
                font-weight: bold;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 3px 0 3px;
            }
            QPushButton {
                background-color: #0066cc;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 6px 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #0052a3;
            }
            QPushButton:pressed {
                background-color: #003d7a;
            }
            QSpinBox, QDoubleSpinBox, QComboBox {
                border: 1px solid #0066cc;
                border-radius: 3px;
                padding: 3px;
                background-color: white;
            }
            QLabel {
                color: #333;
            }
        """)

        self.config = SpectrometerConfig()
        self.spectrometer = ThereminoSpectrometer(self.config)
        self.acquisition_thread: Optional[AcquisitionThread] = None
        self.is_connected = False  # Track connection state
        self.output_folder = str(Path.home() / "Documents" / "Spectra")  # Default output folder
        Path(self.output_folder).mkdir(parents=True, exist_ok=True)
        self.preview_width = 1920  # Original frame width
        self.preview_height = 1080  # Original frame height

        # Create UI
        self.create_menu_bar()
        self.create_toolbar()
        self.create_central_widget()
        self.create_status_bar()

        # Timer for updates
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_display)
        self.timer.start(100)

    def create_menu_bar(self):
        """Create menu bar (File, Tools, Language, Help, About)"""
        menubar = self.menuBar()

        # FILE MENU
        file_menu = menubar.addMenu("File")

        load_data_action = file_menu.addAction("Load data file")
        load_data_action.triggered.connect(self.load_data_file)

        file_menu.addSeparator()

        load_cal_action = file_menu.addAction("Load calibration")
        load_cal_action.triggered.connect(self.load_calibration)

        save_cal_action = file_menu.addAction("Save calibration as")
        save_cal_action.triggered.connect(self.save_calibration)

        file_menu.addSeparator()
        file_menu.addAction("Load irradiance coeffs")
        file_menu.addAction("Edit irradiance coeffs")

        file_menu.addSeparator()
        exit_action = file_menu.addAction("Exit")
        exit_action.triggered.connect(self.close)

        # TOOLS MENU
        tools_menu = menubar.addMenu("Tools")

        smile_action = tools_menu.addAction("Process static image (smile correction)…")
        smile_action.triggered.connect(self.process_static_image)
        tools_menu.addSeparator()

        trim_menu = tools_menu.addMenu("Trim points")
        trim_menu.addAction("Fluorescent 436 546")
        trim_menu.addAction("Fluorescent 436 692")

        sep_menu = tools_menu.addMenu("DataFile Separator")
        sep_menu.addAction("Single TAB")
        sep_menu.addAction("Semicolon and spaces")
        sep_menu.addAction("Comma and spaces")

        type_menu = tools_menu.addMenu("File Type")
        type_menu.addAction("TXT")
        type_menu.addAction("CSV")

        # LANGUAGE MENU
        lang_menu = menubar.addMenu("Language")
        for lang in ["English", "Italiano", "Francais", "Espanol", "Portoguese", "Deutsch", "Russian", "Japanese", "Chinese"]:
            lang_menu.addAction(lang)

        # HELP MENU
        help_menu = menubar.addMenu("Help")
        help_menu.addAction("Help files on Theremino WebSite")
        help_menu.addSeparator()
        help_menu.addAction("Open program folder")

        # ABOUT MENU
        about_action = menubar.addAction("About")
        about_action.triggered.connect(lambda: QMessageBox.information(self, "About", "KOSEN USB Spectroscopy"))

    def create_toolbar(self):
        """Create toolbar with action buttons"""
        toolbar = self.addToolBar("Main Toolbar")
        toolbar.addAction("Save Spectrum")
        toolbar.addAction("Save Total")
        toolbar.addSeparator()
        toolbar.addAction("Save DataFile")
        toolbar.addAction("Repeat")
        toolbar.addAction("Options")
        toolbar.addAction("Info")

    def create_central_widget(self):
        """Create main central widget with layout"""
        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QHBoxLayout(central)

        # LEFT PANEL (Controls)
        left_panel = self.create_left_panel()

        # RIGHT PANEL (Spectrum plot)
        right_panel = self.create_right_panel()

        main_layout.addWidget(left_panel, stretch=0)
        main_layout.addWidget(right_panel, stretch=1)

        central.setLayout(main_layout)

    def create_left_panel(self) -> QWidget:
        """Create left control panel with 2 columns: ROI + Calibration"""
        # Main container
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)

        # Scrollable area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)

        # Panel inside scroll - 2-column grid so every box aligns
        panel = QWidget()
        grid = QGridLayout(panel)
        grid.setSpacing(6)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        top = Qt.AlignmentFlag.AlignTop

        # Row 0: Video Input spans both columns
        grid.addWidget(self.create_video_input_groupbox(), 0, 0, 1, 2)

        # Row 1: ROI | Calibration  (side by side)
        grid.addWidget(self.create_input_groupbox(), 1, 0, top)
        grid.addWidget(self.create_calibration_groupbox(), 1, 1, top)

        # Row 2: Filters | Save
        grid.addWidget(self.create_filters_groupbox(), 2, 0, top)
        grid.addWidget(self.create_save_groupbox(), 2, 1, top)

        # Row 3: absorb remaining vertical space
        grid.setRowStretch(3, 1)

        scroll.setWidget(panel)
        container_layout.addWidget(scroll)

        container.setMaximumWidth(640)
        return container


    def create_video_input_groupbox(self) -> QGroupBox:
        """Video input device selection"""
        group = QGroupBox("Video Input Device")
        layout = QVBoxLayout()

        layout.addWidget(QLabel("Device:"))
        self.combo_video_device = QComboBox()

        # Get camera names (replicates ComboBox_VideoInputDevice_InitWithCurrentDeviceName)
        cameras = self.get_camera_names()
        self.combo_video_device.addItems(cameras)

        layout.addWidget(self.combo_video_device)

        # Capture resolution — higher width = more spectral samples
        res_row = QHBoxLayout()
        res_row.addWidget(QLabel("Resolution:"))
        self.combo_resolution = QComboBox()
        # label -> (width, height); (0,0) = sensor maximum
        self._resolution_options = [
            ("Max (native)", (0, 0)),
            ("3840 × 2160", (3840, 2160)),
            ("1920 × 1080", (1920, 1080)),
            ("1280 × 720", (1280, 720)),
            ("640 × 480", (640, 480)),
        ]
        self.combo_resolution.addItems([o[0] for o in self._resolution_options])
        self.combo_resolution.setToolTip(
            "Higher width = higher spectral resolution.\nApplied on Connect.")
        res_row.addWidget(self.combo_resolution)
        layout.addLayout(res_row)

        self.btn_connect_webcam = QPushButton("Connect WebCam")
        self.btn_connect_webcam.clicked.connect(self.connect_webcam)
        layout.addWidget(self.btn_connect_webcam)

        self.label_webcam_resolution = QLabel("Resolution: -")
        self.label_webcam_fps = QLabel("FPS: -")
        layout.addWidget(self.label_webcam_resolution)
        layout.addWidget(self.label_webcam_fps)

        group.setLayout(layout)
        return group

    def create_input_groupbox(self) -> QGroupBox:
        """Input/ROI group"""
        group = QGroupBox("ROI (Sensor Samples)")
        layout = QFormLayout()
        layout.setSpacing(2)

        self.spin_start_x = QSpinBox()
        self.spin_start_x.setRange(0, 1000)
        self.spin_start_x.setValue(0)
        self.spin_start_x.setMaximumWidth(80)
        layout.addRow("X Start:", self.spin_start_x)

        self.spin_end_x = QSpinBox()
        self.spin_end_x.setRange(0, 1000)
        self.spin_end_x.setValue(1000)
        self.spin_end_x.setMaximumWidth(80)
        layout.addRow("X End:", self.spin_end_x)

        self.spin_start_y = QSpinBox()
        self.spin_start_y.setRange(0, 1000)
        self.spin_start_y.setValue(500)
        self.spin_start_y.setMaximumWidth(80)
        layout.addRow("Y Pos:", self.spin_start_y)

        self.spin_size_y = QSpinBox()
        self.spin_size_y.setRange(1, 1000)
        self.spin_size_y.setValue(100)
        self.spin_size_y.setMaximumWidth(80)
        self.spin_size_y.setToolTip("↑ Larger = more rows averaged = less noise\n↓ Smaller = more detail, more noise")
        layout.addRow("Y Height:", self.spin_size_y)

        label_size_y_info = QLabel("<small>Bigger = smoother spectrum</small>")
        label_size_y_info.setStyleSheet("color: #666; font-size: 10px;")
        layout.addRow("", label_size_y_info)

        self.chk_flip_h = QCheckBox("Flip H")
        self.chk_flip_h.stateChanged.connect(lambda: setattr(self.config, 'flip_h', self.chk_flip_h.isChecked()))
        self.chk_flip_v = QCheckBox("Flip V")
        self.chk_flip_v.stateChanged.connect(lambda: setattr(self.config, 'flip_v', self.chk_flip_v.isChecked()))
        layout.addRow(self.chk_flip_h, self.chk_flip_v)

        # Smile (line curvature) correction
        self.btn_compute_smile = QPushButton("Compute smile map")
        self.btn_compute_smile.setToolTip("Straighten curved spectral lines using the current frame")
        self.btn_compute_smile.clicked.connect(self.compute_smile_map)
        layout.addRow(self.btn_compute_smile)

        self.chk_smile = QCheckBox("Apply smile correction")
        self.chk_smile.setEnabled(False)
        self.chk_smile.stateChanged.connect(self.toggle_smile)
        layout.addRow(self.chk_smile)

        group.setLayout(layout)
        return group

    def create_filters_groupbox(self) -> QGroupBox:
        """Filters group"""
        group = QGroupBox("Filters")
        layout = QFormLayout()
        layout.setSpacing(2)

        self.spin_average = QDoubleSpinBox()
        self.spin_average.setRange(0.0, 1.0)
        self.spin_average.setValue(0.1)
        self.spin_average.setSingleStep(0.01)
        self.spin_average.setMaximumWidth(80)
        layout.addRow("Average α:", self.spin_average)

        self.spin_spatial = QSpinBox()
        self.spin_spatial.setRange(1, 10)
        self.spin_spatial.setValue(5)
        self.spin_spatial.setMaximumWidth(80)
        layout.addRow("Spatial:", self.spin_spatial)

        self.chk_average = QPushButton("Average ON/OFF")
        self.chk_average.setCheckable(True)
        self.chk_average.setChecked(True)
        layout.addRow(self.chk_average)

        self.btn_reference = QPushButton("Reference")
        self.btn_background = QPushButton("Background")
        layout.addRow(self.btn_reference, self.btn_background)

        group.setLayout(layout)
        return group

    def create_calibration_groupbox(self) -> QGroupBox:
        """Calibration group"""
        group = QGroupBox("Wavelength Calibration")
        layout = QFormLayout()
        layout.setSpacing(2)

        self.btn_calibrate_cfl = QPushButton("🔍 Auto CFL")
        self.btn_calibrate_cfl.clicked.connect(self.calibrate_auto_cfl)
        layout.addRow(self.btn_calibrate_cfl)

        layout.addRow(QLabel("<b>Manual:</b>"))
        self.spin_peak1_pixel = QSpinBox()
        self.spin_peak1_pixel.setRange(0, 10000)
        self.spin_peak1_pixel.setValue(500)
        self.spin_peak1_pixel.setMaximumWidth(70)
        self.spin_peak1_nm = QDoubleSpinBox()
        self.spin_peak1_nm.setRange(200, 900)
        self.spin_peak1_nm.setValue(436)
        self.spin_peak1_nm.setMaximumWidth(70)
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Pk1 px:"))
        row1.addWidget(self.spin_peak1_pixel)
        row1.addWidget(QLabel("→ nm:"))
        row1.addWidget(self.spin_peak1_nm)
        layout.addRow(row1)

        self.spin_peak2_pixel = QSpinBox()
        self.spin_peak2_pixel.setRange(0, 10000)
        self.spin_peak2_pixel.setValue(1000)
        self.spin_peak2_pixel.setMaximumWidth(70)
        self.spin_peak2_nm = QDoubleSpinBox()
        self.spin_peak2_nm.setRange(200, 900)
        self.spin_peak2_nm.setValue(546)
        self.spin_peak2_nm.setMaximumWidth(70)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Pk2 px:"))
        row2.addWidget(self.spin_peak2_pixel)
        row2.addWidget(QLabel("→ nm:"))
        row2.addWidget(self.spin_peak2_nm)
        layout.addRow(row2)

        self.btn_apply_manual_calib = QPushButton("Apply")
        self.btn_apply_manual_calib.clicked.connect(self.apply_manual_calibration)
        layout.addRow(self.btn_apply_manual_calib)

        group.setLayout(layout)
        return group

    def create_save_groupbox(self) -> QGroupBox:
        """Save/File group"""
        group = QGroupBox("Save Image")
        layout = QVBoxLayout()

        # Measurement title (prefisso filename)
        layout.addWidget(QLabel("Measurement:"))
        self.text_measurement_title = QLineEdit()
        self.text_measurement_title.setPlaceholderText("e.g. CFL_Calibration (optional)")
        self.text_measurement_title.textChanged.connect(self.update_plot_title)
        layout.addWidget(self.text_measurement_title)

        # Output folder
        folder_layout = QHBoxLayout()
        self.label_output_folder = QLabel("Output: Documents/Spectra")
        folder_layout.addWidget(self.label_output_folder)
        self.btn_select_folder = QPushButton("Browse...")
        self.btn_select_folder.clicked.connect(self.select_output_folder)
        folder_layout.addWidget(self.btn_select_folder)
        layout.addLayout(folder_layout)

        self.combo_file_type = QComboBox()
        self.combo_file_type.addItems(["TXT", "CSV"])
        layout.addWidget(QLabel("File Type:"))
        layout.addWidget(self.combo_file_type)

        save_layout = QHBoxLayout()
        self.btn_save = QPushButton("Save...")
        self.btn_save.clicked.connect(self.save_spectrum)
        save_layout.addWidget(self.btn_save)

        self.btn_screenshot = QPushButton("📸 Quick Screenshot")
        self.btn_screenshot.clicked.connect(self.take_screenshot)
        save_layout.addWidget(self.btn_screenshot)
        layout.addLayout(save_layout)

        group.setLayout(layout)
        return group

    def create_right_panel(self) -> QWidget:
        """Create right panel with spectrum plot"""
        panel = QWidget()
        layout = QVBoxLayout(panel)

        # Spectrum plot (60% height)
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setLabel('bottom', 'Wavelength', units='nm')
        self.plot_widget.setLabel('left', 'Intensity')
        self.plot_widget.setTitle("Spectrum")
        self.plot_widget.setBackground('w')
        self.spectrum_curve = self.plot_widget.plot(pen=pg.mkPen('b', width=2))

        # Lock the plot: not draggable/zoomable. It auto-fits the data (ROI)
        # in real time; the cursor is ONLY for reading coordinates.
        vb = self.plot_widget.getViewBox()
        vb.setMouseEnabled(x=False, y=False)
        vb.setMenuEnabled(False)
        vb.enableAutoRange(x=True, y=True)
        self.plot_widget.hideButtons()
        self.plot_widget.setMouseTracking(True)

        # Read-only crosshair cursor (hidden until the mouse hovers the plot)
        self.vline = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen('r', width=1, style=Qt.PenStyle.DashLine))
        self.hline = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen('r', width=1, style=Qt.PenStyle.DashLine))
        self.plot_widget.addItem(self.vline, ignoreBounds=True)
        self.plot_widget.addItem(self.hline, ignoreBounds=True)
        self.cursor_label = pg.TextItem(text="", anchor=(0, 1), color=(200, 0, 0))
        self.plot_widget.addItem(self.cursor_label)
        self._set_plot_cursor_visible(False)

        # Mouse events on plot (read-only). Leave hides the crosshair.
        self.plot_widget.scene().sigMouseMoved.connect(self.on_plot_mouse_move)
        self.plot_widget.installEventFilter(self)

        layout.addWidget(self.plot_widget, stretch=3)

        # Camera preview section (40% height)
        preview_layout = QVBoxLayout()
        preview_label = QLabel("Camera Preview (with ROI):")
        preview_label.setStyleSheet("font-weight: bold;")
        preview_layout.addWidget(preview_label)

        # Preview label with aspect ratio maintained + interactive cursor
        self.label_preview = InteractivePreviewLabel()
        self.label_preview.setMinimumSize(400, 300)
        self.label_preview.setMaximumHeight(400)
        self.label_preview.setStyleSheet("border: 2px solid #888; background: black;")
        self.label_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label_preview.setScaledContents(False)
        self.label_preview.pixel_clicked.connect(self.on_preview_clicked)
        self.label_preview.mouse_moved.connect(self.on_preview_mouse_move)
        preview_layout.addWidget(self.label_preview, stretch=1)

        layout.addLayout(preview_layout, stretch=2)

        return panel

    def create_status_bar(self):
        """Create status bar"""
        self.statusBar().showMessage("Ready")

    def get_camera_names(self) -> List[str]:
        """Get list of camera names"""
        names = []
        # Try fewer cameras on macOS - usually only 0, 1, 2 exist
        for device_id in range(3):
            try:
                cap = cv2.VideoCapture(device_id)
                if cap.isOpened():
                    # Verify camera works
                    ret, frame = cap.read()
                    if ret and frame is not None:
                        name = f"USB Camera {len(names)}"
                        names.append(name)
                    cap.release()
            except Exception:
                pass

        return names if names else ["USB Camera 0"]

    def connect_webcam(self):
        """Toggle connect/disconnect to selected webcam"""
        if self.is_connected:
            # Disconnect
            if self.acquisition_thread:
                self.acquisition_thread.stop()
                self.acquisition_thread.wait()
                self.acquisition_thread = None
            self.spectrometer.disconnect_webcam()
            self.is_connected = False
            self.btn_connect_webcam.setText("Connect WebCam")
            self.label_webcam_resolution.setText("Resolution: -")
            self.label_webcam_fps.setText("FPS: -")
            self.statusBar().showMessage("Disconnected")
        else:
            # Connect — apply the chosen capture resolution first
            w_req, h_req = self._resolution_options[self.combo_resolution.currentIndex()][1]
            self.config.cam_width = w_req
            self.config.cam_height = h_req

            device_id = self.combo_video_device.currentIndex()
            if self.spectrometer.connect_webcam(device_id):
                self.is_connected = True
                self.btn_connect_webcam.setText("Disconnect WebCam")
                self.label_webcam_resolution.setText(
                    f"Resolution: {self.spectrometer.src_w}×{self.spectrometer.src_h}"
                    f"  ({self.spectrometer.src_w} spectral samples)")
                self.statusBar().showMessage(f"Connected to USB Camera {device_id}")

                # Start acquisition thread
                self.acquisition_thread = AcquisitionThread(self.spectrometer)
                self.acquisition_thread.spectrum_ready.connect(self.on_spectrum_ready)
                self.acquisition_thread.preview_ready.connect(self.on_preview_ready)
                self.acquisition_thread.start()
            else:
                QMessageBox.warning(self, "Error", f"Failed to connect to USB Camera {device_id}")

    def on_spectrum_ready(self, data):
        """Handle new spectrum"""
        x, y = data
        self.update_spectrum_display()

    def on_preview_ready(self, frame):
        """Handle camera preview with ROI overlay"""
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape

        # Draw ROI rectangle on frame
        frame_with_roi = rgb.copy()

        # Calculate ROI coordinates from permille values
        roi_x0 = (w * self.config.start_x) // 1000
        roi_x1 = w - (w * (1000 - self.config.end_x)) // 1000
        roi_y0 = h - (h * self.config.start_y) // 1000 - (h * self.config.size_y) // 1000
        roi_y1 = h - (h * self.config.start_y) // 1000

        # Draw ROI box in green (in image space - always correct)
        cv2.rectangle(frame_with_roi, (roi_x0, roi_y0), (roi_x1, roi_y1), (0, 255, 0), 2)
        cv2.putText(frame_with_roi, "ROI", (roi_x0 + 5, roi_y0 + 20),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

        self.preview_width = w
        self.preview_height = h

        # Convert to QPixmap, scale for display; the crosshair is drawn by the
        # label's paintEvent (Qt) so it tracks the mouse live and stays aligned.
        bytes_per_line = 3 * w
        qt_image = QImage(frame_with_roi.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(qt_image)
        scaled_pixmap = pixmap.scaledToHeight(350, Qt.TransformationMode.SmoothTransformation)
        self.label_preview.set_frame_pixmap(scaled_pixmap, w, h)

    def on_preview_clicked(self, px: int, py: int):
        """Click on preview to set ROI. px,py are ORIGINAL frame coords."""
        orig_w = self.label_preview.orig_w
        orig_h = self.label_preview.orig_h
        if orig_w <= 0 or orig_h <= 0:
            return

        # X: left->right, 0..1000
        x_permille = max(0, min(1000, (px * 1000) // orig_w))
        # Y Pos in config is measured FROM THE BOTTOM, so invert the click y
        y_permille = max(0, min(1000, ((orig_h - py) * 1000) // orig_h))

        self.spin_start_x.setValue(x_permille)
        self.spin_start_y.setValue(y_permille)

        self.statusBar().showMessage(
            f"Clicked px {px}×{py}  ->  X Start={x_permille}‰  Y Pos={y_permille}‰"
        )

    def on_preview_mouse_move(self, px: int, py: int):
        """Mouse moved on preview - show pixel coordinates"""
        # Could add slider visualization here
        pass

    # ========================================================================
    # SMILE (spectral line curvature) CORRECTION
    # ========================================================================

    def compute_smile_map(self):
        """Build a smile-correction map from the current live frame."""
        frame = self.spectrometer.current_frame
        if frame is None:
            QMessageBox.warning(self, "Smile correction",
                                "No frame available. Connect the camera and aim at "
                                "a line-rich source (e.g. a CFL lamp) first.")
            return

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        self.statusBar().showMessage("Computing smile map...")
        QApplication.processEvents()

        corr, msg = smile_correction.compute_smile_correction(rgb)
        if corr is None:
            self.chk_smile.setEnabled(False)
            self.chk_smile.setChecked(False)
            QMessageBox.warning(self, "Smile correction failed", msg)
            self.statusBar().showMessage("Smile map failed")
            return

        self.spectrometer.smile = corr
        self.chk_smile.setEnabled(True)
        self.chk_smile.setChecked(True)   # enables and applies via toggle_smile
        QMessageBox.information(self, "Smile correction", msg)
        self.statusBar().showMessage(msg)

    def toggle_smile(self):
        self.spectrometer.smile_enabled = self.chk_smile.isChecked()
        state = "ON" if self.chk_smile.isChecked() else "OFF"
        self.statusBar().showMessage(f"Smile correction {state}")

    def process_static_image(self):
        """Load an image file, straighten its spectral lines, save the result."""
        filename, _ = QFileDialog.getOpenFileName(
            self, "Select spectrum image", self.output_folder,
            "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff);;All Files (*)"
        )
        if not filename:
            return

        img_bgr = cv2.imread(filename, cv2.IMREAD_COLOR)
        if img_bgr is None:
            QMessageBox.warning(self, "Error", f"Could not read image:\n{filename}")
            return

        rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        self.statusBar().showMessage("Straightening image...")
        QApplication.processEvents()

        corrected, before, after, msg = smile_correction.straighten_image(rgb)
        if corrected is None:
            QMessageBox.warning(self, "Smile correction failed", msg)
            return

        stem = Path(filename).with_suffix("")
        out_path = f"{stem}_straightened.png"
        cv2.imwrite(out_path, cv2.cvtColor(corrected, cv2.COLOR_RGB2BGR))

        self.statusBar().showMessage(f"Saved {Path(out_path).name}")
        QMessageBox.information(
            self, "Smile correction",
            f"{msg}\n\nStraightened image saved to:\n{out_path}")

    def _set_plot_cursor_visible(self, visible: bool):
        self.vline.setVisible(visible)
        self.hline.setVisible(visible)
        self.cursor_label.setVisible(visible)

    def eventFilter(self, obj, event):
        """Hide the plot crosshair when the mouse leaves the plot widget."""
        if obj is self.plot_widget and event.type() == QEvent.Type.Leave:
            self._set_plot_cursor_visible(False)
        return super().eventFilter(obj, event)

    def on_plot_mouse_move(self, pos):
        """Read-only crosshair on the plot. X axis is nm when calibrated."""
        if not self.plot_widget.sceneBoundingRect().contains(pos):
            self._set_plot_cursor_visible(False)
            return

        self._set_plot_cursor_visible(True)
        mousePoint = self.plot_widget.plotItem.vb.mapSceneToView(pos)
        x = mousePoint.x()
        y = mousePoint.y()

        self.vline.setPos(x)
        self.hline.setPos(y)

        # X axis is wavelength when calibrated, else pixel column
        if len(self.spectrometer.calib_bin) >= 2:
            px = self.spectrometer.nm_to_bin(x)
            text = f"λ:{x:.1f}nm  px:{px:.0f}"
        else:
            text = f"px:{x:.0f}"

        self.cursor_label.setText(text)
        self.cursor_label.setPos(x, y)

    def update_display(self):
        """Update config from UI"""
        self.config.start_x = self.spin_start_x.value()
        self.config.end_x = self.spin_end_x.value()
        self.config.start_y = self.spin_start_y.value()
        self.config.size_y = self.spin_size_y.value()
        self.config.average_filter_alpha = self.spin_average.value()
        self.config.spatial_averaging = self.spin_spatial.value()

    # ========================================================================
    # FILE OPERATIONS
    # ========================================================================

    def load_data_file(self):
        """Load data file"""
        filename, _ = QFileDialog.getOpenFileName(
            self, "Load Data File", "",
            "CSV Files (*.csv);;TXT Files (*.txt);;All Files (*)"
        )
        if filename:
            if self.spectrometer.load_data_file(filename):
                self.statusBar().showMessage(f"Loaded: {Path(filename).name}")
                self.update_spectrum_display()
            else:
                QMessageBox.warning(self, "Error", "Failed to load file")

    def load_calibration(self):
        """Load calibration file"""
        filename, _ = QFileDialog.getOpenFileName(
            self, "Load Calibration", "",
            "INI Files (*.ini);;All Files (*)"
        )
        if filename:
            if self.spectrometer.load_calibration(filename):
                self.statusBar().showMessage(f"Calibration loaded: {Path(filename).name}")
                self.label_calibration = QLabel(f"✓ {Path(filename).stem}")
            else:
                QMessageBox.warning(self, "Error", "Failed to load calibration")

    def save_calibration(self):
        """Save calibration file"""
        filename, _ = QFileDialog.getSaveFileName(
            self, "Save Calibration", "",
            "INI Files (*.ini)"
        )
        if filename:
            if self.spectrometer.save_calibration(filename):
                self.statusBar().showMessage(f"Calibration saved: {Path(filename).name}")
            else:
                QMessageBox.warning(self, "Error", "Failed to save calibration")

    def select_output_folder(self):
        """Select output folder for saving"""
        folder = QFileDialog.getExistingDirectory(
            self, "Select Output Folder", self.output_folder
        )
        if folder:
            self.output_folder = folder
            self.label_output_folder.setText(f"Output: {Path(folder).name}")
            self.statusBar().showMessage(f"Output folder: {folder}")

    def update_plot_title(self):
        """Update plot title based on measurement name"""
        title = self.text_measurement_title.text().strip()
        if title:
            self.plot_widget.setTitle(f"Spectrum - {title}")
        else:
            self.plot_widget.setTitle("Spectrum")

    def take_screenshot(self):
        """Take quick screenshot of spectrum and save with optional title prefix"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        title = self.text_measurement_title.text().strip()

        # Build filename: [Title_]timestamp.png
        if title:
            # Replace spaces with underscores
            title_safe = title.replace(" ", "_")
            filename = Path(self.output_folder) / f"{title_safe}_{timestamp}.png"
        else:
            filename = Path(self.output_folder) / f"spectrum_{timestamp}.png"

        # Save spectrum plot as image
        exporter = pg.exporters.ImageExporter(self.plot_widget.plotItem)
        exporter.export(str(filename))

        self.statusBar().showMessage(f"Screenshot saved: {filename.name}")
        QMessageBox.information(self, "Screenshot Saved", f"Saved to:\n{filename}")

    def save_spectrum(self):
        """Save current spectrum to CSV/TXT with title prefix and timestamp"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        title = self.text_measurement_title.text().strip()

        # Build default filename
        if title:
            title_safe = title.replace(" ", "_")
            default_name = f"{title_safe}_{timestamp}"
        else:
            default_name = f"spectrum_{timestamp}"

        file_type = self.combo_file_type.currentText()
        ext = "csv" if file_type == "CSV" else "txt"

        filename, _ = QFileDialog.getSaveFileName(
            self, "Save Spectrum Data",
            str(Path(self.output_folder) / f"{default_name}.{ext}"),
            f"{file_type} Files (*.{ext.lower()})"
        )
        if filename:
            if self.spectrometer.save_spectrum_csv(filename):
                self.statusBar().showMessage(f"Saved: {Path(filename).name}")
            else:
                QMessageBox.warning(self, "Error", "Failed to save spectrum")

    def apply_manual_calibration(self):
        """Apply manual calibration from spinboxes"""
        peak1_pixel = float(self.spin_peak1_pixel.value())
        peak1_nm = float(self.spin_peak1_nm.value())
        peak2_pixel = float(self.spin_peak2_pixel.value())
        peak2_nm = float(self.spin_peak2_nm.value())

        if peak1_pixel >= peak2_pixel:
            QMessageBox.warning(self, "Error", "Peak 1 pixel must be < Peak 2 pixel")
            return

        # Set calibration points
        self.spectrometer.calib_bin = np.array([peak1_pixel, peak2_pixel])
        self.spectrometer.calib_nm = np.array([peak1_nm, peak2_nm])

        msg = (f"Manual Calibration Applied:\n"
               f"Peak 1: pixel {peak1_pixel:.0f} → {peak1_nm:.1f}nm\n"
               f"Peak 2: pixel {peak2_pixel:.0f} → {peak2_nm:.1f}nm")
        QMessageBox.information(self, "Calibration Applied", msg)
        self.statusBar().showMessage("Manual calibration applied")

    def calibrate_auto_cfl(self):
        """Auto-calibrate using CFL spectrum (finds all peaks, assigns to 436+546nm)"""
        # Get current spectrum
        spectrum = self.spectrometer.array_visible_samples
        if len(spectrum) == 0:
            QMessageBox.warning(self, "Error", "No spectrum data available. Connect camera first.")
            return

        # Find ALL peaks with dynamic threshold (like Spectral Workbench)
        max_val = np.max(spectrum)
        if max_val < 10:
            QMessageBox.warning(self, "Error", "Spectrum too weak. Increase illumination.")
            return

        # Find peaks with 20% threshold of max
        peaks, properties = find_peaks(spectrum, height=max_val * 0.2, distance=30, prominence=max_val * 0.1)

        if len(peaks) < 2:
            QMessageBox.warning(self, "Error", f"Found only {len(peaks)} peak(s). Need at least 2 for CFL calibration.\nIncrease illumination.")
            return

        # Sort by height and get top 2, converting ROI indices -> absolute pixel columns
        peak_heights = spectrum[peaks]
        top_2_indices = np.argsort(peak_heights)[-2:]
        top_2_peaks = sorted(peaks[top_2_indices])

        roi_x0 = self.spectrometer.roi_x0
        peak1_pixel = float(top_2_peaks[0] + roi_x0)
        peak2_pixel = float(top_2_peaks[1] + roi_x0)

        # CFL spectrum lines: 436nm (blue) and 546nm (green)
        peak1_nm = 436.0
        peak2_nm = 546.0

        # Set calibration points
        self.spectrometer.calib_bin = np.array([peak1_pixel, peak2_pixel])
        self.spectrometer.calib_nm = np.array([peak1_nm, peak2_nm])

        # Show all detected peaks
        msg = (f"CFL Auto-Calibration (found {len(peaks)} peaks):\n\n"
               f"Using Top 2 Peaks:\n"
               f"Peak 1: pixel {peak1_pixel:.0f} → {peak1_nm:.0f}nm (blue)\n"
               f"Peak 2: pixel {peak2_pixel:.0f} → {peak2_nm:.0f}nm (green)\n\n"
               f"All peaks found at pixels: {', '.join([f'{p:.0f}' for p in peaks])}")
        QMessageBox.information(self, "CFL Auto-Calibration", msg)
        self.statusBar().showMessage(f"CFL auto-calibrated ({len(peaks)} peaks found)")

    # ========================================================================
    # DISPLAY UPDATE
    # ========================================================================

    def update_spectrum_display(self):
        """Update spectrum plot. X axis = absolute pixel column (or nm if calibrated).
        Because the ROI now crops the array, X Start/X End change the plotted span."""
        y = self.spectrometer.array_visible_samples
        if len(y) == 0:
            return

        # Absolute pixel column for each sample (left edge = roi_x0)
        pixels = self.spectrometer.roi_x0 + np.arange(len(y))

        # If calibrated (2+ points), map pixels -> wavelength for the X axis
        if len(self.spectrometer.calib_bin) >= 2:
            x = np.array([self.spectrometer.bin_to_nm(float(p)) for p in pixels])
            self.plot_widget.setLabel('bottom', 'Wavelength', units='nm')
        else:
            x = pixels.astype(float)
            self.plot_widget.setLabel('bottom', 'Pixel')

        if self.config.log_scale > 1:
            y = np.log(np.maximum(y, 1e-6)) / np.log(self.config.log_scale)

        self.spectrum_curve.setData(x, y)

    # ========================================================================
    # CLOSEVENT
    # ========================================================================

    def closeEvent(self, event):
        """Handle window close"""
        if self.acquisition_thread:
            self.acquisition_thread.stop()
        if self.spectrometer.camera:
            self.spectrometer.disconnect_webcam()
        event.accept()


# ============================================================================
# MAIN
# ============================================================================

def main():
    app = QApplication(sys.argv)
    window = ThereminoSpectrometryGUI()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
