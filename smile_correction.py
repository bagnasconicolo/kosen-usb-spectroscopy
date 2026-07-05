"""
Smile (spectral line curvature) correction.

Diffraction-grating spectra show curved emission lines ("smile" distortion):
the same wavelength lands at slightly different x on different rows. Averaging
the ROI vertically then smears the peaks. This module straightens each row so
that a given wavelength sits at the same x on every row, making the vertical
average valid and the peaks sharper.

The geometric distortion is fixed by the optics, so the warp is computed ONCE
from a reference frame and stored as an OpenCV remap map. Applying it to each
live frame is then a single, fast ``cv2.remap`` call.

Based on a row-by-row peak-matching + polynomial-fit algorithm.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
import cv2
from scipy.signal import find_peaks
from scipy.interpolate import interp1d
from scipy.optimize import linear_sum_assignment


@dataclass
class SmileParams:
    reference_half_height: int = 8      # rows around the centre used as reference
    min_peaks_per_row: int = 4          # min matched peaks to fit a row
    max_match_distance: int = 25        # px window to match a row peak to the reference
    poly_degree: int = 2                # 1 = shift+scale, 2 = adds slight curvature
    smooth_sigma: float = 2.0           # x-smoothing before peak finding


# ----------------------------------------------------------------------------
# low level helpers (kept close to the original static-image script)
# ----------------------------------------------------------------------------

def _gaussian_kernel_1d(sigma, radius=None):
    if radius is None:
        radius = int(4 * sigma + 0.5)
    x = np.arange(-radius, radius + 1)
    k = np.exp(-(x ** 2) / (2 * sigma ** 2))
    k /= k.sum()
    return k


def _smooth_1d(signal, sigma=2.0):
    return np.convolve(signal, _gaussian_kernel_1d(sigma), mode="same")


def _luminance(row_rgb):
    """Rec.709 luminance; lines are spread across R,G,B so use luma."""
    row = row_rgb.astype(np.float32)
    return 0.2126 * row[:, 0] + 0.7152 * row[:, 1] + 0.0722 * row[:, 2]


def _find_spectral_peaks(profile, smooth_sigma=2.0):
    prof = _smooth_1d(profile, sigma=smooth_sigma)
    baseline = np.percentile(prof, 20)
    amplitude = np.percentile(prof, 99) - baseline
    if amplitude <= 0:
        return np.array([], dtype=int)
    threshold = baseline + 0.18 * amplitude
    peaks, _ = find_peaks(prof, height=threshold,
                          prominence=0.08 * amplitude, distance=12)
    return peaks


def _match_peaks(row_peaks, ref_peaks, max_dist=25):
    if len(row_peaks) == 0 or len(ref_peaks) == 0:
        return np.array([]), np.array([])
    cost = np.abs(row_peaks[:, None] - ref_peaks[None, :])
    r_ind, c_ind = linear_sum_assignment(cost)
    good = cost[r_ind, c_ind] <= max_dist
    matched_row = row_peaks[r_ind[good]]
    matched_ref = ref_peaks[c_ind[good]]
    order = np.argsort(matched_row)
    return matched_row[order], matched_ref[order]


def _fit_row_xref(row_peaks, ref_peaks, width, degree=2):
    """x_ref[x] = where old pixel x must move to align with the reference row."""
    n = len(row_peaks)
    if n < 2:
        return None
    deg = min(degree, n - 1)
    poly = np.poly1d(np.polyfit(row_peaks, ref_peaks, deg))
    return poly(np.arange(width, dtype=np.float32))


def _xref_to_sample_map(x_ref, width):
    """Invert x_ref to get, for each OUTPUT column, the SOURCE column to read.
    Returns a length-`width` array (NaN where undefined)."""
    x_old = np.arange(width, dtype=np.float32)
    order = np.argsort(x_ref)
    xref_sorted = x_ref[order]
    xold_sorted = x_old[order]
    uniq_xref, idx = np.unique(xref_sorted, return_index=True)
    if len(uniq_xref) < 2:
        return None
    inverse = interp1d(uniq_xref, xold_sorted[idx], kind="linear",
                       bounds_error=False, fill_value=np.nan)
    return inverse(np.arange(width, dtype=np.float32))


# ----------------------------------------------------------------------------
# the reusable correction object
# ----------------------------------------------------------------------------

class SmileCorrection:
    """Holds an OpenCV remap map that straightens spectral lines."""

    def __init__(self, map_x, map_y, ref_peaks, used_rows, total_rows, shape):
        self.map_x = map_x            # HxW float32 : source column per output pixel
        self.map_y = map_y            # HxW float32 : source row (identity)
        self.ref_peaks = ref_peaks
        self.used_rows = used_rows
        self.total_rows = total_rows
        self.shape = shape            # (H, W) the map was built for

    def matches(self, frame_rgb) -> bool:
        return frame_rgb.shape[:2] == self.shape

    def apply(self, frame_rgb):
        """Return a straightened copy of the frame (RGB or BGR, any 3-channel)."""
        if frame_rgb.shape[:2] != self.shape:
            return frame_rgb
        return cv2.remap(frame_rgb, self.map_x, self.map_y,
                         interpolation=cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_REPLICATE)


def compute_smile_correction(img_rgb, params: Optional[SmileParams] = None
                             ) -> Tuple[Optional[SmileCorrection], str]:
    """Compute a SmileCorrection from a reference image/frame.

    Returns (correction, message). `correction` is None on failure and the
    message explains why.
    """
    p = params or SmileParams()
    if img_rgb is None or img_rgb.ndim != 3:
        return None, "Invalid image."

    h, w, _ = img_rgb.shape

    # reference profile from a strip around the centre row
    yc = h // 2
    y0 = max(0, yc - p.reference_half_height)
    y1 = min(h, yc + p.reference_half_height + 1)
    strip = img_rgb[y0:y1]
    ref_profile = np.mean([_luminance(strip[i]) for i in range(strip.shape[0])], axis=0)
    ref_peaks = _find_spectral_peaks(ref_profile, p.smooth_sigma)

    if len(ref_peaks) < p.min_peaks_per_row:
        return None, (f"Only {len(ref_peaks)} reference peaks found "
                      f"(need {p.min_peaks_per_row}). Improve contrast / lighting "
                      f"or point at a line-rich source (e.g. a CFL lamp).")

    identity = np.arange(w, dtype=np.float32)
    map_x = np.zeros((h, w), dtype=np.float32)
    map_y = np.repeat(np.arange(h, dtype=np.float32)[:, None], w, axis=1)

    last_good = None
    used = 0
    for y in range(h):
        profile = _luminance(img_rgb[y])
        peaks = _find_spectral_peaks(profile, p.smooth_sigma)
        mrow, mref = _match_peaks(peaks, ref_peaks, p.max_match_distance)

        sample = None
        if len(mrow) >= p.min_peaks_per_row:
            x_ref = _fit_row_xref(mrow, mref, w, p.poly_degree)
            if x_ref is not None:
                sample = _xref_to_sample_map(x_ref, w)

        if sample is None:
            sample = last_good if last_good is not None else identity.copy()
        else:
            last_good = sample
            used += 1

        # NaN (undefined) -> read in place so remap leaves those pixels untouched
        bad = ~np.isfinite(sample)
        if bad.any():
            sample = sample.copy()
            sample[bad] = identity[bad]

        map_x[y] = sample

    corr = SmileCorrection(map_x, map_y, ref_peaks, used, h, (h, w))
    return corr, f"Smile map built: {used}/{h} rows fitted, {len(ref_peaks)} reference peaks."


def straighten_image(img_rgb, params: Optional[SmileParams] = None):
    """Static-image pipeline. Returns (corrected_rgb, before_profile, after_profile,
    message). corrected_rgb is None on failure."""
    corr, msg = compute_smile_correction(img_rgb, params)
    if corr is None:
        return None, None, None, msg
    corrected = corr.apply(img_rgb)
    before = np.mean([_luminance(img_rgb[y]) for y in range(img_rgb.shape[0])], axis=0)
    after = np.mean([_luminance(corrected[y]) for y in range(corrected.shape[0])], axis=0)
    return corrected, before, after, msg
