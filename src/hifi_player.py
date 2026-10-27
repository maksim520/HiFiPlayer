#!/usr/bin/env python3
"""
HiFi Master v2.0 - High-Fidelity Audio Player
==============================================
PC HiFi audio player featuring:
- ASIO / WASAPI Exclusive bit-perfect output
- Memory playback (entire file loaded to RAM)
- High-quality sinc interpolation oversampling (up to 8x)
- Professional TPDF and noise-shaped dithering
- FLAC, WAV, AIFF, OGG, DSF (DSD) support
- Full playlist management with drag-and-drop
- Modern dark-themed UI
"""

import sys
import os
import platform
import time
import threading
import math
import struct
from pathlib import Path
from typing import Optional, Tuple, List, Callable

import numpy as np
import sounddevice as sd
import soundfile as sf
import tkinter as tk
from tkinter import filedialog, ttk, messagebox

# Platform detection
IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"

# Windows-specific imports and setup
if IS_WINDOWS:
    try:
        import ctypes
        from ctypes import wintypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

APP_NAME = "HiFi Master"
APP_VERSION = "2.1.0"
AUDIO_EXTS = {".flac", ".wav", ".aiff", ".ogg", ".dsf", ".ape", ".wv", ".opus"}

SUPPORTED_FORMATS = [
    ("HiFi Audio", "*.flac *.wav *.aiff *.ogg *.dsf"),
    ("FLAC", "*.flac"),
    ("WAV", "*.wav"),
    ("DSF (DSD)", "*.dsf"),
    ("All Files", "*.*"),
]


# ============================================================
# Windows Audio Optimizations
# ============================================================

class WindowsAudioOptimizations:
    """Windows-specific audio quality optimizations:
    - MMCSS Pro Audio thread priority
    - VirtualLock memory page locking
    - Process priority boost
    - CPU core parking prevention
    """

    _mmcss_handle = None
    _memory_locked = False

    @staticmethod
    def set_process_priority_high():
        """Raise process priority to Above Normal for smoother audio."""
        if not IS_WINDOWS:
            return
        try:
            import ctypes
            ABOVE_NORMAL = 0x00008000
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(), ABOVE_NORMAL)
        except Exception:
            pass

    @staticmethod
    def set_mmcss_pro_audio():
        """Set current thread to MMCSS 'Pro Audio' priority (level 23-26).
        Must be called from the audio callback thread."""
        if not IS_WINDOWS:
            return
        try:
            import ctypes
            from ctypes import wintypes
            avrt = ctypes.windll.avrt
            task_index = wintypes.DWORD(0)
            handle = avrt.AvSetMmThreadCharacteristicsW(
                "Pro Audio", ctypes.byref(task_index))
            if handle:
                avrt.AvSetMmThreadPriority(handle, 2)  # AVRT_PRIORITY_CRITICAL
                WindowsAudioOptimizations._mmcss_handle = handle
        except Exception:
            pass

    @staticmethod
    def revert_mmcss():
        """Revert MMCSS thread priority."""
        if not IS_WINDOWS:
            return
        try:
            import ctypes
            handle = WindowsAudioOptimizations._mmcss_handle
            if handle:
                ctypes.windll.avrt.AvRevertMmThreadCharacteristics(handle)
                WindowsAudioOptimizations._mmcss_handle = None
        except Exception:
            pass

    @staticmethod
    def lock_memory(buffer_array):
        """Lock audio buffer pages in RAM to prevent page faults during playback.
        Avoids hard page faults that cause audio glitches."""
        if not IS_WINDOWS or buffer_array is None:
            return False
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32

            # First increase working set size (required for VirtualLock)
            process = kernel32.GetCurrentProcess()
            buf_bytes = buffer_array.nbytes
            min_ws = max(buf_bytes * 2, 16 * 1024 * 1024)  # At least 16MB
            kernel32.SetProcessWorkingSetSize(process, min_ws, min_ws * 2)

            # Lock the buffer pages
            result = kernel32.VirtualLock(
                buffer_array.ctypes.data, buffer_array.nbytes)
            if result:
                WindowsAudioOptimizations._memory_locked = True
            return bool(result)
        except Exception:
            return False

    @staticmethod
    def unlock_memory(buffer_array):
        """Unlock previously locked memory pages."""
        if not IS_WINDOWS or buffer_array is None:
            return
        try:
            import ctypes
            ctypes.windll.kernel32.VirtualUnlock(
                buffer_array.ctypes.data, buffer_array.nbytes)
            WindowsAudioOptimizations._memory_locked = False
        except Exception:
            pass

    @staticmethod
    def optimize_power():
        """Set high performance power settings during playback."""
        if not IS_WINDOWS:
            return
        try:
            import subprocess
            # Try to activate Ultimate Performance power plan
            result = subprocess.run(
                ['powercfg', '-duplicatescheme',
                 'e9a42b02-d5df-448d-aa00-03f14749eb61'],
                capture_output=True, text=True)
            # Extract the new scheme GUID from output
            if result.returncode == 0 and result.stderr:
                lines = result.stderr.strip().split('\n')
                for line in lines:
                    if ':' in line:
                        guid = line.split(':')[-1].strip()
                        subprocess.run(
                            ['powercfg', '/setactive', guid],
                            capture_output=True)
                        return
            # Fallback: set current plan to High Performance
            subprocess.run(
                ['powercfg', '/setactive', '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c'],
                capture_output=True)
        except Exception:
            pass


# ============================================================
# DSP: Sinc Oversampler
# ============================================================

class SincOversampler:
    def __init__(self, num_taps=8192):
        self.num_taps = num_taps

    def design_filter(self, num_taps: int, cutoff: float) -> np.ndarray:
        if num_taps % 2 == 0:
            num_taps += 1
        n = np.arange(num_taps) - (num_taps - 1) / 2.0
        with np.errstate(divide="ignore", invalid="ignore"):
            h = np.sinc(2.0 * cutoff * n)
        window = (
            0.35875
            - 0.48829 * np.cos(2 * np.pi * n / (num_taps - 1))
            + 0.14128 * np.cos(4 * np.pi * n / (num_taps - 1))
            - 0.01168 * np.cos(6 * np.pi * n / (num_taps - 1))
        )
        h *= window
        h /= np.sum(h)
        return h

    def oversample(self, data: np.ndarray, factor: int) -> np.ndarray:
        if factor == 1:
            return data.copy()
        n_samples, n_channels = data.shape
        n_output = n_samples * factor
        cutoff = 0.9 / factor
        filt = self.design_filter(min(self.num_taps, 16384), cutoff)
        filt *= factor
        stuffed = np.zeros((n_output, n_channels), dtype=np.float64)
        stuffed[::factor] = data
        fft_size = 1
        while fft_size < n_output + len(filt):
            fft_size *= 2
        H = np.fft.rfft(filt, fft_size)
        result = np.zeros((n_output, n_channels), dtype=np.float64)
        for ch in range(n_channels):
            X = np.fft.rfft(stuffed[:, ch], fft_size)
            result[:, ch] = np.fft.irfft(X * H, fft_size)[:n_output]
        return result


# ============================================================
# DSP: Dither Engine
# ============================================================

class DitherEngine:
    @staticmethod
    def apply_tpdf(data: np.ndarray, target_bits: int = 24) -> np.ndarray:
        if target_bits >= 32:
            return data.copy()
        lsb = 1.0 / (2 ** (target_bits - 1))
        rng = np.random.default_rng()
        dither = (rng.random(data.shape) + rng.random(data.shape) - 1.0) * lsb
        scale = 2 ** (target_bits - 1)
        return np.clip(np.round((data + dither) * scale) / scale, -1.0, 1.0)

    @staticmethod
    def apply_noise_shaped(data: np.ndarray, target_bits: int = 24, sr: int = 44100) -> np.ndarray:
        if target_bits >= 32:
            return data.copy()
        n_samples, n_channels = data.shape
        lsb = 1.0 / (2 ** (target_bits - 1))
        scale = 2 ** (target_bits - 1)
        b1, b2 = -1.622, 0.642
        rng = np.random.default_rng()
        result = np.empty_like(data)
        for ch in range(n_channels):
            e1, e2 = 0.0, 0.0
            ch_data = data[:, ch]
            ch_result = np.empty(n_samples, dtype=np.float64)
            dithers = (rng.random(n_samples) + rng.random(n_samples) - 1.0) * lsb
            for i in range(n_samples):
                shaped = ch_data[i] + dithers[i] - (b1 * e1 + b2 * e2)
                quantized = round(shaped * scale) / scale
                err = shaped - quantized
                e2, e1 = e1, err
                ch_result[i] = min(max(quantized, -1.0), 1.0)
            result[:, ch] = ch_result
        return result


# ============================================================
# DSF Reader
# ============================================================

class DSFReader:
    @staticmethod
    def read(filepath: str) -> Tuple[np.ndarray, int]:
        """Read DSF file and convert to PCM (lossy - uses decimation filter)."""
        with open(filepath, "rb") as f:
            if f.read(4) != b"DSD ":
                raise ValueError("Not a valid DSF file")
            f.read(8)
            if f.read(4) != b"fmt ":
                raise ValueError("Missing fmt chunk")
            fmt_size = struct.unpack("<Q", f.read(8))[0]
            fmt_data = f.read(fmt_size - 12)
            off = 0
            _ver = struct.unpack_from("<I", fmt_data, off)[0]; off += 4
            _fid = struct.unpack_from("<I", fmt_data, off)[0]; off += 4
            _cht = struct.unpack_from("<I", fmt_data, off)[0]; off += 4
            ch_count = struct.unpack_from("<I", fmt_data, off)[0]; off += 4
            dsd_sr = struct.unpack_from("<I", fmt_data, off)[0]; off += 4
            _bps = struct.unpack_from("<I", fmt_data, off)[0]; off += 4
            sample_count = struct.unpack_from("<Q", fmt_data, off)[0]; off += 8
            block_size = struct.unpack_from("<I", fmt_data, off)[0]; off += 4
            chunk_id = f.read(4)
            while chunk_id != b"data":
                skip = struct.unpack("<Q", f.read(8))[0]
                f.seek(skip - 12, 1)
                chunk_id = f.read(4)
            data_size = struct.unpack("<Q", f.read(8))[0]
            raw = f.read(data_size - 12)
        pcm_sr = 352800 if dsd_sr >= 5644800 else (176400 if dsd_sr >= 2822400 else 88200)
        decimation = dsd_sr // pcm_sr
        bytes_per_block_ch = block_size // ch_count
        n_blocks = (sample_count + block_size * 8 - 1) // (block_size * 8)
        channels = []
        for ch in range(ch_count):
            ch_bytes = bytearray()
            for blk in range(n_blocks):
                o = blk * block_size + ch * bytes_per_block_ch
                ch_bytes.extend(raw[o:o + bytes_per_block_ch])
            arr = np.frombuffer(bytes(ch_bytes[:sample_count // 8 + 1]), dtype=np.uint8)
            bits = np.unpackbits(arr)[:sample_count].astype(np.float64) * 2.0 - 1.0
            cutoff = 0.9 / decimation
            ntaps = min(8192, len(bits) // 4)
            if ntaps < 128: ntaps = 128
            n = np.arange(ntaps) - (ntaps - 1) / 2.0
            with np.errstate(divide="ignore", invalid="ignore"):
                lp = np.sinc(2.0 * cutoff * n)
            lp *= np.blackman(ntaps)
            lp /= np.sum(lp)
            fft_sz = 1
            while fft_sz < len(bits) + ntaps: fft_sz *= 2
            filtered = np.fft.irfft(np.fft.rfft(bits, fft_sz) * np.fft.rfft(lp, fft_sz), fft_sz)[:len(bits)]
            channels.append(filtered[::decimation])
        min_len = min(len(c) for c in channels)
        data = np.column_stack([c[:min_len] for c in channels])
        peak = np.max(np.abs(data))
        if peak > 0: data = data / peak * 0.95
        return data, pcm_sr

    @staticmethod
    def read_raw(filepath: str) -> dict:
        """Read DSF file and return raw DSD data (for DoP/DSD Native - lossless).
        Returns dict with: channels (list of uint8 arrays), ch_count, dsd_sr, sample_count, block_size"""
        with open(filepath, "rb") as f:
            if f.read(4) != b"DSD ":
                raise ValueError("Not a valid DSF file")
            f.read(8)
            if f.read(4) != b"fmt ":
                raise ValueError("Missing fmt chunk")
            fmt_size = struct.unpack("<Q", f.read(8))[0]
            fmt_data = f.read(fmt_size - 12)
            off = 0
            _ver = struct.unpack_from("<I", fmt_data, off)[0]; off += 4
            _fid = struct.unpack_from("<I", fmt_data, off)[0]; off += 4
            _cht = struct.unpack_from("<I", fmt_data, off)[0]; off += 4
            ch_count = struct.unpack_from("<I", fmt_data, off)[0]; off += 4
            dsd_sr = struct.unpack_from("<I", fmt_data, off)[0]; off += 4
            _bps = struct.unpack_from("<I", fmt_data, off)[0]; off += 4
            sample_count = struct.unpack_from("<Q", fmt_data, off)[0]; off += 8
            block_size = struct.unpack_from("<I", fmt_data, off)[0]; off += 4
            chunk_id = f.read(4)
            while chunk_id != b"data":
                skip = struct.unpack("<Q", f.read(8))[0]
                f.seek(skip - 12, 1)
                chunk_id = f.read(4)
            data_size = struct.unpack("<Q", f.read(8))[0]
            raw = f.read(data_size - 12)
        bytes_per_block_ch = block_size // ch_count
        n_blocks = (sample_count + block_size * 8 - 1) // (block_size * 8)
        channels = []
        for ch in range(ch_count):
            ch_bytes = bytearray()
            for blk in range(n_blocks):
                o = blk * block_size + ch * bytes_per_block_ch
                ch_bytes.extend(raw[o:o + bytes_per_block_ch])
            channels.append(np.frombuffer(bytes(ch_bytes), dtype=np.uint8).copy())
        return {
            "channels": channels,
            "ch_count": ch_count,
            "dsd_sr": dsd_sr,
            "sample_count": sample_count,
            "block_size": block_size,
        }


# ============================================================
# DoP Encoder (DSD over PCM - Lossless DSD Output)
# ============================================================

class DoPEncoder:
    """Encode raw DSD data into DoP (DSD over PCM) format.
    DoP packs DSD bits into PCM frames with a marker byte.
    The DAC recognizes the marker and extracts DSD directly — no conversion loss.

    DoP frame structure (per channel, per sample period):
      Byte 0: Marker (0x05 for DSD, 0xFA for alternate)
      Byte 1: Marker (0x05 / 0xFA)
      Byte 2-3: DSD audio data (MSB first)

    For stereo: L-frame, R-frame interleaved.
    """

    DOP_MARKER = 0x05
    DOP_MARKER_ALT = 0xFA

    @staticmethod
    def encode(dsd_channels: List[np.ndarray], dsd_sr: int) -> Tuple[np.ndarray, int]:
        """Encode raw DSD channel data into DoP PCM stream.

        Args:
            dsd_channels: List of uint8 arrays, one per channel (raw DSD bytes)
            dsd_sr: DSD sample rate (e.g. 2822400 for DSD64)

        Returns:
            (dop_data, pcm_sr) where dop_data is float32 PCM array for sounddevice,
            pcm_sr is the DoP carrier sample rate
        """
        ch_count = len(dsd_channels)
        min_bytes = min(len(ch) for ch in dsd_channels)
        # DoP packs 2 DSD bytes per frame (16 DSD bits per PCM sample period)
        # Each DoP frame = 1 marker byte + 1 marker byte + 2 DSD bytes = 4 bytes = 24bit PCM sample
        n_frames = min_bytes // 2  # 2 DSD bytes per frame

        # DoP carrier rate = DSD rate / 16 (16 bits per PCM sample in DoP)
        # DSD64 (2.8224MHz) → 176.4kHz, DSD128 → 352.8kHz, DSD256 → 705.6kHz
        pcm_sr = dsd_sr // 16

        # Build DoP frames as int32 array
        # Each frame: 0x05 0x05 DSD_byte1 DSD_byte2 (big-endian 32-bit word)
        dop_frames = np.zeros((n_frames, ch_count), dtype=np.int32)

        for ch_idx in range(ch_count):
            ch_data = dsd_channels[ch_idx][:n_frames * 2]
            for i in range(n_frames):
                b0 = ch_data[i * 2]       # first DSD byte
                b1 = ch_data[i * 2 + 1]   # second DSD byte
                # Pack: marker | marker | DSD_byte1 | DSD_byte2
                # Big-endian: 0x05 0x05 xx xx
                frame = (DoPEncoder.DOP_MARKER << 24) | (DoPEncoder.DOP_MARKER << 16) | (b0 << 8) | b1
                dop_frames[i, ch_idx] = frame

        # Convert int32 to float32 for sounddevice output
        # Scale to [-1.0, 1.0) range preserving all 32 bits
        dop_float = dop_frames.astype(np.float64) / 2147483648.0
        dop_float = dop_float.astype(np.float32)

        return dop_float, pcm_sr


# ============================================================
# Audio Engine
# ============================================================

class AudioEngine:
    def __init__(self):
        self._data = None
        self._processed = None
        self._sr = 44100
        self._processed_sr = 44100
        self._channels = 2
        self._pos = 0
        self._playing = False
        self._paused = False
        self._stream = None
        self._lock = threading.Lock()
        self._on_end = None
        self._volume = 0.3
        self._target_volume = 0.3  # for smooth ramp
        self._output_mode = "Unknown"
        self._track_end_flag = False
        self._mmcss_set = False
        self._is_dop = False  # True when playing DoP (DSD over PCM)
        self._is_dsd_native = False  # True when playing DSD Native via ASIO
        self.oversample_factor = 1
        self.dither_mode = "none"
        self.dither_bits = 24
        self.bit_perfect = True
        self.dsd_mode = "dop"  # "dop" | "native" | "pcm"
        self._oversampler = SincOversampler(num_taps=8192)
        self._ditherer = DitherEngine()
        # Anti-denormal offset: ~-200dB, inaudible but prevents CPU spikes
        self._anti_denormal = 1e-10

    def load_file(self, filepath: str) -> dict:
        ext = Path(filepath).suffix.lower()
        self._is_dop = False
        self._is_dsd_native = False
        if ext in (".flac", ".wav", ".aiff", ".ogg"):
            data, sr = sf.read(filepath, dtype="float64", always_2d=True)
        elif ext == ".dsf":
            if self.dsd_mode == "dop":
                # DoP mode: DSD over PCM (lossless, universal)
                raw = DSFReader.read_raw(filepath)
                data, sr = DoPEncoder.encode(raw["channels"], raw["dsd_sr"])
                self._is_dop = True
                self._channels = len(raw["channels"])
                if self._processed is not None:
                    WindowsAudioOptimizations.unlock_memory(self._processed)
                self._data = data; self._sr = sr; self._processed_sr = sr
                self._pos = 0; self._playing = False; self._paused = False
                self._track_end_flag = False
                self._processed = data
                WindowsAudioOptimizations.lock_memory(self._processed)
                return {
                    "sample_rate": raw["dsd_sr"], "channels": self._channels,
                    "duration": raw["sample_count"] / raw["dsd_sr"],
                    "samples": raw["sample_count"], "format": "DoP/DSD",
                    "dsd_rate": raw["dsd_sr"], "dop_carrier": sr,
                }
            elif self.dsd_mode == "native":
                # DSD Native: raw 1-bit DSD stream via ASIO (lossless, ASIO + DSD DAC only)
                raw = DSFReader.read_raw(filepath)
                ch_count = len(raw["channels"])
                # Interleave raw DSD bytes for all channels
                min_bytes = min(len(ch) for ch in raw["channels"])
                interleaved = np.zeros((min_bytes, ch_count), dtype=np.uint8)
                for ch_idx in range(ch_count):
                    interleaved[:, ch_idx] = raw["channels"][ch_idx][:min_bytes]
                # DSD native rate = DSD bit rate / 8 (byte rate)
                # The ASIO driver expects data at the DSD byte rate
                dsd_byte_sr = raw["dsd_sr"] // 8
                self._is_dsd_native = True
                self._channels = ch_count
                if self._processed is not None:
                    WindowsAudioOptimizations.unlock_memory(self._processed)
                # Store as float32 for sounddevice (will be cast to uint8 by ASIO)
                self._data = interleaved.astype(np.float32) / 255.0
                self._sr = dsd_byte_sr; self._processed_sr = dsd_byte_sr
                self._pos = 0; self._playing = False; self._paused = False
                self._track_end_flag = False
                self._processed = self._data
                WindowsAudioOptimizations.lock_memory(self._processed)
                return {
                    "sample_rate": raw["dsd_sr"], "channels": ch_count,
                    "duration": raw["sample_count"] / raw["dsd_sr"],
                    "samples": raw["sample_count"], "format": "DSD Native",
                    "dsd_rate": raw["dsd_sr"],
                }
            else:
                # DSD→PCM mode: convert with decimation filter (lossy, fallback)
                data, sr = DSFReader.read(filepath)
        else:
            raise ValueError(f"Unsupported format: {ext}")
        # Standard PCM path
        if self._processed is not None:
            WindowsAudioOptimizations.unlock_memory(self._processed)
        self._data = data; self._sr = sr
        self._channels = data.shape[1]
        self._pos = 0; self._playing = False; self._paused = False
        self._track_end_flag = False
        self._preprocess()
        WindowsAudioOptimizations.lock_memory(self._processed)
        return {
            "sample_rate": sr, "channels": self._channels,
            "duration": len(data) / sr, "samples": len(data), "format": ext[1:].upper(),
        }

    def _preprocess(self):
        data = self._data.copy()
        if self.bit_perfect:
            self._processed = data.astype(np.float32)
            self._processed_sr = self._sr
            return
        if self.oversample_factor > 1:
            data = self._oversampler.oversample(data, self.oversample_factor)
            self._processed_sr = self._sr * self.oversample_factor
        else:
            self._processed_sr = self._sr
        if self.dither_mode == "tpdf":
            data = self._ditherer.apply_tpdf(data, self.dither_bits)
        elif self.dither_mode == "noise_shaped":
            data = self._ditherer.apply_noise_shaped(data, self.dither_bits, self._processed_sr)
        self._processed = data.astype(np.float32)

    def set_oversample(self, factor: int):
        assert factor in (1, 2, 4, 8)
        self.oversample_factor = factor
        if self._data is not None: self._preprocess()

    def set_dither(self, mode: str, bits: int = 24):
        assert mode in ("none", "tpdf", "noise_shaped")
        self.dither_mode = mode; self.dither_bits = bits
        if self._data is not None: self._preprocess()

    def set_bit_perfect(self, enabled: bool):
        self.bit_perfect = enabled
        if self._data is not None: self._preprocess()

    def set_volume(self, vol: float):
        self._target_volume = max(0.0, min(1.0, vol))

    def play(self, device=None, callback=None):
        if self._processed is None: return
        self._on_end = callback
        self._track_end_flag = False
        if self._paused:
            with self._lock:
                self._playing = True; self._paused = False
            if self._stream is None:
                self._start_stream(device)
            return
        self.stop()
        with self._lock:
            self._playing = True; self._paused = False
        # Apply Windows audio optimizations
        WindowsAudioOptimizations.set_process_priority_high()
        WindowsAudioOptimizations.optimize_power()
        WindowsAudioOptimizations.lock_memory(self._processed)
        self._start_stream(device)

    def _start_stream(self, device=None):
        is_asio = False
        dev_info = None
        if device is not None:
            try:
                dev_info = sd.query_devices(device)
                is_asio = sd.query_hostapis(dev_info["hostapi"])["name"] == "ASIO"
            except Exception:
                pass

        output_sr = self._processed_sr
        self._output_mode = "Unknown"
        errors = []

        if IS_MACOS:
            # macOS: CoreAudio handles bit-perfect natively
            # No ASIO/WASAPI, just open at file's native rate
            for attempt in range(2):
                try:
                    if attempt == 0:
                        self._stream = sd.OutputStream(
                            samplerate=output_sr, channels=self._channels, dtype="float32",
                            device=device, callback=self._audio_callback, blocksize=0,
                        )
                        self._stream.start()
                        self._output_mode = "CoreAudio"
                        return
                    else:
                        # Fallback: resample to device default rate
                        if dev_info:
                            native_sr = int(dev_info.get("default_samplerate", 48000))
                            if native_sr != output_sr:
                                self._processed = self._resample_processed(native_sr)
                                self._processed_sr = native_sr
                                output_sr = native_sr
                        self._stream = sd.OutputStream(
                            samplerate=output_sr, channels=self._channels, dtype="float32",
                            device=device, callback=self._audio_callback,
                        )
                        self._stream.start()
                        self._output_mode = "CoreAudio (Resampled)"
                        return
                except Exception as e:
                    errors.append(f"Attempt {attempt}: {e}")
                    continue
        else:
            # Windows: DSD Native (ASIO only), then ASIO, WASAPI Exclusive, WASAPI Shared, Resampled
            # DSD Native requires ASIO — skip other modes for native DSD
            if self._is_dsd_native and is_asio:
                try:
                    self._stream = sd.OutputStream(
                        samplerate=output_sr, channels=self._channels, dtype="float32",
                        device=device, callback=self._audio_callback, blocksize=1024,
                    )
                    self._stream.start()
                    self._output_mode = "ASIO DSD Native"
                    return
                except Exception as e:
                    # DSD Native failed, fall through to other modes
                    errors.append(f"DSD Native: {e}")
                    self._is_dsd_native = False

            for attempt in range(4):
                try:
                    if attempt == 0 and is_asio:
                        self._stream = sd.OutputStream(
                            samplerate=output_sr, channels=self._channels, dtype="float32",
                            device=device, callback=self._audio_callback, blocksize=1024,
                        )
                        self._stream.start()
                        if self._is_dsd_native:
                            self._output_mode = "ASIO DSD Native"
                        elif self._is_dop:
                            self._output_mode = "ASIO + DoP"
                        else:
                            self._output_mode = "ASIO"
                        return
                    elif attempt == 1:
                        self._stream = sd.OutputStream(
                            samplerate=output_sr, channels=self._channels, dtype="float32",
                            device=device, callback=self._audio_callback, blocksize=0,
                            extra_settings=sd.WasapiSettings(exclusive=True),
                        )
                        self._stream.start()
                        if self._is_dop:
                            self._output_mode = "WASAPI Excl. + DoP"
                        else:
                            self._output_mode = "WASAPI Exclusive"
                        return
                    elif attempt == 2:
                        self._stream = sd.OutputStream(
                            samplerate=output_sr, channels=self._channels, dtype="float32",
                            device=device, callback=self._audio_callback,
                        )
                        self._stream.start()
                        self._output_mode = "WASAPI Shared"
                        return
                    else:
                        if dev_info:
                            native_sr = int(dev_info.get("default_samplerate", 48000))
                            if native_sr != output_sr:
                                self._processed = self._resample_processed(native_sr)
                                self._processed_sr = native_sr
                                output_sr = native_sr
                        self._stream = sd.OutputStream(
                            samplerate=output_sr, channels=self._channels, dtype="float32",
                            device=device, callback=self._audio_callback,
                        )
                        self._stream.start()
                        self._output_mode = "Resampled"
                        return
                except Exception as e:
                    errors.append(f"Attempt {attempt}: {e}")
                    continue

        self._playing = False
        raise RuntimeError(f"Cannot open audio device: {'; '.join(errors)}")

    def _resample_processed(self, target_sr: int) -> np.ndarray:
        """Resample processed data to target sample rate using linear interpolation."""
        if self._processed is None:
            return None
        src_len = len(self._processed)
        ratio = target_sr / self._processed_sr
        new_len = int(src_len * ratio)
        indices = np.linspace(0, src_len - 1, new_len)
        result = np.zeros((new_len, self._channels), dtype=np.float32)
        for ch in range(self._channels):
            result[:, ch] = np.interp(indices, np.arange(src_len), self._processed[:, ch])
        return result

    def _audio_callback(self, outdata, frames, time_info, status):
        try:
            # Set MMCSS Pro Audio priority on first callback (runs on PortAudio thread)
            if IS_WINDOWS and not self._mmcss_set:
                WindowsAudioOptimizations.set_mmcss_pro_audio()
                self._mmcss_set = True

            with self._lock:
                if self._processed is None:
                    outdata[:] = 0; return
                if self._paused:
                    outdata[:] = 0; return
                if not self._playing:
                    outdata[:] = 0; return

                pos = self._pos; end = pos + frames; dlen = len(self._processed)

                # DoP/DSD Native: data cannot be multiplied (would corrupt marker/DSD bytes)
                # Output raw data directly, volume must be controlled at DAC/hardware level
                if self._is_dop or self._is_dsd_native:
                    if end <= dlen:
                        outdata[:] = self._processed[pos:end]
                        self._pos = end
                    else:
                        rem = dlen - pos
                        if rem > 0: outdata[:rem] = self._processed[pos:]
                        outdata[rem:] = 0
                        self._playing = False; self._paused = False; self._pos = 0
                        self._track_end_flag = True
                    return

                # Normal PCM mode: smooth volume ramp to avoid clicks (64-sample ramp)
                current_vol = self._volume
                target_vol = self._target_volume

                if current_vol != target_vol:
                    n_ramp = min(64, frames)
                    ramp = np.linspace(current_vol, target_vol, n_ramp, dtype=np.float32)
                    if end <= dlen:
                        chunk = self._processed[pos:end]
                        outdata[:n_ramp] = chunk[:n_ramp] * ramp[:, np.newaxis]
                        if frames > n_ramp:
                            outdata[n_ramp:] = chunk[n_ramp:] * target_vol
                        self._pos = end
                    else:
                        rem = dlen - pos
                        if rem > 0:
                            chunk = self._processed[pos:]
                            apply_ramp = min(rem, n_ramp)
                            outdata[:apply_ramp] = chunk[:apply_ramp] * ramp[:apply_ramp, np.newaxis]
                            if rem > apply_ramp:
                                outdata[apply_ramp:rem] = chunk[apply_ramp:] * target_vol
                        outdata[rem:] = 0
                        self._playing = False; self._paused = False; self._pos = 0
                        self._track_end_flag = True
                    self._volume = target_vol
                else:
                    if end <= dlen:
                        outdata[:] = self._processed[pos:end] * current_vol
                        self._pos = end
                    else:
                        rem = dlen - pos
                        if rem > 0: outdata[:rem] = self._processed[pos:] * current_vol
                        outdata[rem:] = 0
                        self._playing = False; self._paused = False; self._pos = 0
                        self._track_end_flag = True
        except Exception:
            outdata[:] = 0

    def pause(self):
        with self._lock:
            self._paused = True

    def resume(self):
        with self._lock:
            self._paused = False

    def toggle_pause(self):
        with self._lock:
            self._paused = not self._paused

    def stop(self):
        with self._lock:
            self._playing = False; self._paused = False; self._pos = 0
            self._track_end_flag = False
        if self._stream is not None:
            try: self._stream.stop(); self._stream.close()
            except Exception: pass
            self._stream = None
            WindowsAudioOptimizations.revert_mmcss()
            self._mmcss_set = False

    def seek(self, ratio: float):
        if self._processed is not None:
            with self._lock:
                self._pos = int(ratio * len(self._processed))

    def get_position(self) -> Tuple[float, float]:
        if self._processed is None: return (0.0, 0.0)
        with self._lock:
            return (self._pos / self._processed_sr, len(self._processed) / self._processed_sr)

    def is_playing(self) -> bool:
        with self._lock: return self._playing and not self._paused

    def is_paused(self) -> bool:
        with self._lock: return self._paused

    @staticmethod
    def get_output_devices() -> List[Tuple[int, str, bool]]:
        devices = []
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_output_channels"] > 0:
                # Check if ASIO by hostapi, not by name (XMOS etc. don't have "ASIO" in name)
                is_asio = sd.query_hostapis(dev["hostapi"])["name"] == "ASIO"
                devices.append((i, dev["name"], is_asio))
        return devices

    @staticmethod
    def get_file_info(filepath: str) -> Optional[dict]:
        """Quick read of file metadata without loading full audio."""
        ext = Path(filepath).suffix.lower()
        try:
            if ext in (".flac", ".wav", ".aiff", ".ogg"):
                info = sf.info(filepath)
                return {"format": ext[1:].upper(), "sample_rate": info.samplerate,
                        "channels": info.channels, "duration": info.duration,
                        "subtype": info.subtype}
            elif ext == ".dsf":
                return {"format": "DSF", "sample_rate": 0, "channels": 0,
                        "duration": 0, "subtype": "DSD"}
        except Exception:
            pass
        return None


# ============================================================
# Playlist
# ============================================================

class Playlist:
    def __init__(self):
        self._tracks: List[str] = []
        self._current_index = -1

    def add(self, filepath: str):
        fp = str(Path(filepath).resolve())
        if fp not in self._tracks:
            self._tracks.append(fp)

    def add_many(self, filepaths: List[str]):
        for fp in filepaths:
            self.add(fp)

    def remove(self, index: int):
        if 0 <= index < len(self._tracks):
            del self._tracks[index]
            if self._current_index >= len(self._tracks):
                self._current_index = len(self._tracks) - 1
            if self._current_index == index:
                self._current_index = -1

    def clear(self):
        self._tracks.clear(); self._current_index = -1

    def current(self) -> Optional[str]:
        if 0 <= self._current_index < len(self._tracks):
            return self._tracks[self._current_index]
        return None

    def current_index(self) -> int:
        return self._current_index

    def set_current(self, index: int):
        if 0 <= index < len(self._tracks):
            self._current_index = index

    def next(self) -> Optional[str]:
        if self._current_index < len(self._tracks) - 1:
            self._current_index += 1
            return self._tracks[self._current_index]
        return None

    def prev(self) -> Optional[str]:
        if self._current_index > 0:
            self._current_index -= 1
            return self._tracks[self._current_index]
        return None

    def tracks(self) -> List[str]:
        return self._tracks.copy()

    def count(self) -> int:
        return len(self._tracks)

    def is_empty(self) -> bool:
        return len(self._tracks) == 0


# ============================================================
# GUI
# ============================================================

class HiFiPlayerGUI:
    # Color palette
    BG = "#0d1117"
    BG2 = "#161b22"
    BG3 = "#21262d"
    FG = "#c9d1d9"
    FG2 = "#8b949e"
    ACCENT = "#58a6ff"
    GREEN = "#3fb950"
    RED = "#f85149"
    PURPLE = "#bc8cff"
    ORANGE = "#d29922"
    BORDER = "#30363d"

    def __init__(self):
        self.engine = AudioEngine()
        self.playlist = Playlist()
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry("960x720")
        self.root.minsize(860, 620)
        self.root.configure(bg=self.BG)

        self._device_map = {}
        self._loading = False
        self._selected_index = -1

        # Variables
        self.now_playing = tk.StringVar(value="No track selected")
        self.track_info = tk.StringVar(value="")
        self.position_var = tk.DoubleVar(value=0)
        self.time_display = tk.StringVar(value="00:00 / 00:00")
        self.device_var = tk.StringVar()
        self.oversample_var = tk.StringVar(value="1x (Bit-Perfect)")
        self.dither_var = tk.StringVar(value="None")
        self.dither_bits_var = tk.StringVar(value="24")
        self.bit_perfect_var = tk.BooleanVar(value=True)
        self.dsd_mode_var = tk.StringVar(value="DoP (Lossless)")
        self.status_var = tk.StringVar(value="Ready")
        self.volume_var = tk.DoubleVar(value=30)
        self.playlist_count = tk.StringVar(value="0 tracks")

        self._setup_styles()
        self._build_gui()
        self._refresh_devices()
        self._update_timer()

    def _setup_styles(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure(".", background=self.BG, foreground=self.FG, font=("Segoe UI", 10),
                     borderwidth=0, focusthickness=0)
        s.configure("TFrame", background=self.BG)
        s.configure("TLabel", background=self.BG, foreground=self.FG)
        s.configure("Dark.TFrame", background=self.BG2)
        s.configure("Title.TLabel", background=self.BG2, foreground="#ffffff", font=("Segoe UI", 13, "bold"))
        s.configure("NowPlaying.TLabel", background=self.BG2, foreground=self.ACCENT, font=("Segoe UI", 12, "bold"))
        s.configure("Info.TLabel", background=self.BG2, foreground=self.FG2, font=("Segoe UI", 9))
        s.configure("Time.TLabel", background=self.BG2, foreground=self.GREEN, font=("Consolas", 12, "bold"))
        s.configure("Status.TLabel", background=self.BG3, foreground=self.FG2, font=("Consolas", 9))
        s.configure("Playlist.TLabel", background=self.BG2, foreground=self.FG2, font=("Segoe UI", 9))
        s.configure("TButton", background=self.BG3, foreground=self.FG, font=("Segoe UI", 10),
                     borderwidth=1, relief="flat", padding=(8, 4))
        s.map("TButton", background=[("active", self.ACCENT), ("pressed", "#1f6feb")],
              foreground=[("active", "#ffffff")])
        s.configure("Play.TButton", background="#238636", foreground="#ffffff", font=("Segoe UI", 11, "bold"),
                     padding=(14, 6))
        s.map("Play.TButton", background=[("active", "#2ea043"), ("pressed", "#238636")])
        s.configure("Nav.TButton", background=self.BG3, foreground=self.FG, font=("Segoe UI", 12),
                     padding=(6, 3))
        s.map("Nav.TButton", background=[("active", self.ACCENT)])
        s.configure("TScale", background=self.BG2, troughcolor=self.BG3, sliderthickness=14)
        s.configure("TLabelframe", background=self.BG, foreground=self.FG2, font=("Segoe UI", 9))
        s.configure("TLabelframe.Label", background=self.BG, foreground=self.FG2)
        s.configure("TCheckbutton", background=self.BG, foreground=self.FG)
        s.configure("TCombobox", fieldbackground=self.BG3, background=self.BG3,
                     foreground=self.FG, selectbackground=self.ACCENT)
        s.map("TCombobox", fieldbackground=[("readonly", self.BG3)])

    def _build_gui(self):
        # ===== TOP BAR =====
        top = tk.Frame(self.root, bg=self.BG2, height=54)
        top.pack(fill=tk.X); top.pack_propagate(False)

        tk.Label(top, text=f" {APP_NAME}", bg=self.BG2, fg="#ffffff",
                 font=("Segoe UI", 15, "bold")).pack(side=tk.LEFT, padx=(14, 4), pady=10)
        tk.Label(top, text="v2.1", bg=self.BG2, fg=self.FG2,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=0, pady=10)
        self._sr_label = tk.Label(top, text="Output: --", bg=self.BG2, fg=self.GREEN,
                                   font=("Consolas", 10, "bold"))
        self._sr_label.pack(side=tk.RIGHT, padx=14)
        self._mode_label = tk.Label(top, text="Bit-Perfect", bg=self.BG2, fg=self.PURPLE,
                                     font=("Segoe UI", 9, "bold"))
        self._mode_label.pack(side=tk.RIGHT, padx=6)

        # ===== NOW PLAYING + TRANSPORT =====
        transport = tk.Frame(self.root, bg=self.BG2, pady=8)
        transport.pack(fill=tk.X, padx=0)

        # Track name
        tk.Label(transport, textvariable=self.now_playing, bg=self.BG2, fg=self.ACCENT,
                 font=("Segoe UI", 11, "bold"), anchor=tk.W).pack(fill=tk.X, padx=18, pady=(2, 0))
        tk.Label(transport, textvariable=self.track_info, bg=self.BG2, fg=self.FG2,
                 font=("Segoe UI", 9), anchor=tk.W).pack(fill=tk.X, padx=18, pady=(0, 6))

        # Nav buttons
        nav = tk.Frame(transport, bg=self.BG2)
        nav.pack(fill=tk.X, padx=14)

        ttk.Button(nav, text="\u23EE", style="Nav.TButton", command=self._prev_track, width=3).pack(side=tk.LEFT, padx=2)
        self.play_btn = ttk.Button(nav, text="\u25B6  Play", style="Play.TButton", command=self._play)
        self.play_btn.pack(side=tk.LEFT, padx=4)
        ttk.Button(nav, text="\u23F8", style="Nav.TButton", command=self._pause, width=3).pack(side=tk.LEFT, padx=2)
        ttk.Button(nav, text="\u23F9", style="Nav.TButton", command=self._stop, width=3).pack(side=tk.LEFT, padx=2)
        ttk.Button(nav, text="\u23ED", style="Nav.TButton", command=self._next_track, width=3).pack(side=tk.LEFT, padx=2)

        # Time
        tk.Label(nav, textvariable=self.time_display, bg=self.BG2, fg=self.GREEN,
                 font=("Consolas", 12, "bold")).pack(side=tk.RIGHT, padx=10)

        # Volume - large and prominent, right side
        vol_frame = tk.Frame(nav, bg=self.BG2)
        vol_frame.pack(side=tk.RIGHT, padx=(10, 4))
        self.vol_icon = tk.Label(vol_frame, text="\U0001F508", bg=self.BG2, fg=self.GREEN,
                                  font=("Segoe UI", 14))
        self.vol_icon.pack(side=tk.LEFT, padx=(0, 2))
        self.vol_scale = ttk.Scale(vol_frame, from_=0, to=100, variable=self.volume_var,
                                    orient=tk.HORIZONTAL, length=180, command=self._on_volume)
        self.vol_scale.pack(side=tk.LEFT, padx=4)
        self.vol_label = tk.Label(vol_frame, text="30%", bg=self.BG2, fg=self.ORANGE,
                                   font=("Consolas", 12, "bold"), width=5)
        self.vol_label.pack(side=tk.LEFT)

        # Progress bar
        self.pos_slider = ttk.Scale(transport, from_=0, to=1000, variable=self.position_var,
                                     orient=tk.HORIZONTAL, command=self._on_seek)
        self.pos_slider.pack(fill=tk.X, padx=18, pady=(4, 0))

        # Separator
        tk.Frame(self.root, bg=self.BORDER, height=1).pack(fill=tk.X)

        # ===== MAIN AREA: Playlist + Settings =====
        main = tk.Frame(self.root, bg=self.BG)
        main.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        # -- LEFT: Playlist --
        left = tk.Frame(main, bg=self.BG2, width=520)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 4), pady=8)
        left.pack_propagate(False)

        pl_header = tk.Frame(left, bg=self.BG2)
        pl_header.pack(fill=tk.X, padx=10, pady=(8, 4))
        tk.Label(pl_header, text="Playlist", bg=self.BG2, fg="#ffffff",
                 font=("Segoe UI", 11, "bold")).pack(side=tk.LEFT)
        tk.Label(pl_header, textvariable=self.playlist_count, bg=self.BG2, fg=self.FG2,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=8)
        ttk.Button(pl_header, text="+ Add Files", command=self._add_files).pack(side=tk.RIGHT, padx=2)
        ttk.Button(pl_header, text="+ Add Folder", command=self._add_folder).pack(side=tk.RIGHT, padx=2)
        ttk.Button(pl_header, text="Clear", command=self._clear_playlist).pack(side=tk.RIGHT, padx=2)

        # Playlist listbox with custom styling
        pl_frame = tk.Frame(left, bg=self.BG)
        pl_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self.playlist_box = tk.Listbox(
            pl_frame, bg=self.BG, fg=self.FG, selectbackground=self.ACCENT,
            selectforeground="#ffffff", font=("Consolas", 10), activestyle="none",
            borderwidth=0, highlightthickness=0, relief="flat",
            selectmode=tk.SINGLE, cursor="hand2",
        )
        scrollbar = ttk.Scrollbar(pl_frame, orient=tk.VERTICAL, command=self.playlist_box.yview)
        self.playlist_box.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.playlist_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.playlist_box.bind("<Double-Button-1>", self._on_playlist_dblclick)
        self.playlist_box.bind("<<ListboxSelect>>", self._on_playlist_select)
        self.playlist_box.bind("<Delete>", self._on_playlist_delete)

        # -- RIGHT: Settings --
        right = tk.Frame(main, bg=self.BG, width=340)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=(4, 8), pady=8)
        right.pack_propagate(False)

        # DSP section
        dsp = tk.LabelFrame(right, text=" DSP Pipeline ", bg=self.BG2, fg=self.FG2,
                             font=("Segoe UI", 9, "bold"), padx=10, pady=8)
        dsp.pack(fill=tk.X, pady=(0, 6))

        self.bp_check = tk.Checkbutton(dsp, text="Bit-Perfect Mode",
                                        variable=self.bit_perfect_var, command=self._on_dsp_change,
                                        bg=self.BG2, fg=self.ACCENT, selectcolor=self.BG2,
                                        activebackground=self.BG2, activeforeground=self.ACCENT,
                                        highlightthickness=0,
                                        font=("Segoe UI", 9, "bold"))
        self.bp_check.pack(anchor=tk.W, pady=(0, 4))

        dsd_r = tk.Frame(dsp, bg=self.BG2); dsd_r.pack(fill=tk.X, pady=2)
        tk.Label(dsd_r, text="DSD Mode", bg=self.BG2, fg=self.PURPLE,
                 font=("Segoe UI", 9, "bold"), width=10, anchor=tk.W).pack(side=tk.LEFT)
        ttk.Combobox(dsd_r, textvariable=self.dsd_mode_var,
                      values=["DoP (Lossless)", "DSD Native (ASIO)", "DSD\u2192PCM (Lossy)"],
                      state="readonly", width=14).pack(side=tk.LEFT, padx=4)

        r1 = tk.Frame(dsp, bg=self.BG2); r1.pack(fill=tk.X, pady=2)
        tk.Label(r1, text="Oversample", bg=self.BG2, fg=self.FG2, font=("Segoe UI", 9), width=10, anchor=tk.W).pack(side=tk.LEFT)
        ttk.Combobox(r1, textvariable=self.oversample_var,
                      values=["1x (Bit-Perfect)", "2x", "4x", "8x"], state="readonly", width=14).pack(side=tk.LEFT, padx=4)

        r2 = tk.Frame(dsp, bg=self.BG2); r2.pack(fill=tk.X, pady=2)
        tk.Label(r2, text="Dither", bg=self.BG2, fg=self.FG2, font=("Segoe UI", 9), width=10, anchor=tk.W).pack(side=tk.LEFT)
        ttk.Combobox(r2, textvariable=self.dither_var,
                      values=["None", "TPDF", "Noise-Shaped"], state="readonly", width=14).pack(side=tk.LEFT, padx=4)

        r3 = tk.Frame(dsp, bg=self.BG2); r3.pack(fill=tk.X, pady=2)
        tk.Label(r3, text="Bits", bg=self.BG2, fg=self.FG2, font=("Segoe UI", 9), width=10, anchor=tk.W).pack(side=tk.LEFT)
        ttk.Combobox(r3, textvariable=self.dither_bits_var,
                      values=["16", "24"], state="readonly", width=6).pack(side=tk.LEFT, padx=4)

        # Apply DSP button
        ttk.Button(dsp, text="Apply DSP Settings", command=self._on_dsp_change).pack(fill=tk.X, pady=(8, 0))

        # Output Device section
        dev = tk.LabelFrame(right, text=" Output Device ", bg=self.BG2, fg=self.FG2,
                             font=("Segoe UI", 9, "bold"), padx=10, pady=8)
        dev.pack(fill=tk.X, pady=(0, 6))

        self.device_combo = ttk.Combobox(dev, textvariable=self.device_var, state="readonly", width=38)
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_changed)
        self.device_combo.pack(fill=tk.X, pady=2)
        ttk.Button(dev, text="Refresh Devices", command=self._refresh_devices).pack(fill=tk.X, pady=(4, 0))

        self.dev_info_var = tk.StringVar(value="Select ASIO device for best quality")
        tk.Label(dev, textvariable=self.dev_info_var, bg=self.BG2, fg=self.GREEN,
                 font=("Segoe UI", 8), wraplength=280).pack(anchor=tk.W, pady=(4, 0))

        # Format info section
        fmt = tk.LabelFrame(right, text=" Track Info ", bg=self.BG2, fg=self.FG2,
                             font=("Segoe UI", 9, "bold"), padx=10, pady=8)
        fmt.pack(fill=tk.X, pady=(0, 6))

        self.fmt_text = tk.Text(fmt, bg=self.BG, fg=self.FG2, font=("Consolas", 9),
                                 height=6, width=34, borderwidth=0, relief="flat",
                                 state=tk.DISABLED, wrap=tk.WORD)
        self.fmt_text.pack(fill=tk.X)

        # ===== STATUS BAR =====
        sb = tk.Frame(self.root, bg=self.BG3, height=26)
        sb.pack(fill=tk.X, side=tk.BOTTOM); sb.pack_propagate(False)
        self._status_label = tk.Label(sb, textvariable=self.status_var, bg=self.BG3, fg=self.FG2,
                                       font=("Consolas", 9), anchor=tk.W)
        self._status_label.pack(fill=tk.X, padx=12, pady=3)

        # Key bindings
        self.root.bind("<space>", lambda e: self._toggle_pause())
        self.root.bind("<Return>", lambda e: self._play())
        self.root.bind("<Escape>", lambda e: self._stop())
        self.root.bind("<Right>", lambda e: self._next_track())
        self.root.bind("<Left>", lambda e: self._prev_track())
        self.root.bind("<Up>", lambda e: self._adjust_volume(5))
        self.root.bind("<Down>", lambda e: self._adjust_volume(-5))

    # ---- Playlist ----

    def _add_files(self):
        files = filedialog.askopenfilenames(title="Add Audio Files", filetypes=SUPPORTED_FORMATS)
        if files:
            self._add_to_playlist(list(files))

    def _add_folder(self):
        folder = filedialog.askdirectory(title="Add Music Folder")
        if not folder: return
        found = []
        for ext in AUDIO_EXTS:
            found.extend(str(p) for p in Path(folder).rglob(f"*{ext}"))
            found.extend(str(p) for p in Path(folder).rglob(f"*{ext.upper()}"))
        if found:
            self._add_to_playlist(sorted(found))
        else:
            messagebox.showinfo("Info", "No supported audio files found.")

    def _add_to_playlist(self, filepaths: List[str]):
        added = 0
        for fp in filepaths:
            if Path(fp).suffix.lower() in AUDIO_EXTS:
                self.playlist.add(fp)
                added += 1
        if added > 0:
            self._refresh_playlist_box()
            self.status_var.set(f"Added {added} tracks to playlist")
            if self.playlist.current_index() < 0:
                self.playlist.set_current(0)
                self._load_current()

    def _clear_playlist(self):
        self.engine.stop()
        self.playlist.clear()
        self._refresh_playlist_box()
        self.now_playing.set("No track selected")
        self.track_info.set("")
        self.status_var.set("Playlist cleared")

    def _refresh_playlist_box(self):
        self.playlist_box.delete(0, tk.END)
        for i, fp in enumerate(self.playlist.tracks()):
            name = Path(fp).stem
            ext = Path(fp).suffix[1:].upper()
            # Quick info
            info = AudioEngine.get_file_info(fp)
            if info and info.get("duration", 0) > 0:
                dur = self._fmt(info["duration"])
                sr = f"{info['sample_rate']:,}" if info["sample_rate"] else "?"
                label = f"  {name}  [{ext} {sr}Hz]  {dur}"
            else:
                label = f"  {name}  [{ext}]"
            self.playlist_box.insert(tk.END, label)
            if i == self.playlist.current_index():
                self.playlist_box.itemconfig(i, fg=self.ACCENT)
        self.playlist_count.set(f"{self.playlist.count()} tracks")

    def _on_playlist_dblclick(self, event):
        sel = self.playlist_box.curselection()
        if sel:
            self.playlist.set_current(sel[0])
            self._load_current()
            self._play()

    def _on_playlist_select(self, event):
        """Single click selects a track — pressing Play will play this track."""
        sel = self.playlist_box.curselection()
        if sel:
            self._selected_index = sel[0]

    def _on_playlist_delete(self, event):
        sel = self.playlist_box.curselection()
        if sel:
            self.playlist.remove(sel[0])
            self._refresh_playlist_box()

    # ---- File loading ----

    def _load_current(self):
        fp = self.playlist.current()
        if fp:
            self._load_file(fp)

    def _load_file(self, filepath: str):
        self._loading = True
        self.status_var.set(f"Loading {Path(filepath).name}...")
        self.root.update()
        try:
            info = self.engine.load_file(filepath)
            self.now_playing.set(Path(filepath).stem)
            sr = info["sample_rate"]
            sr_txt = f"{sr:,} Hz" if isinstance(sr, int) else str(sr)
            ch_txt = f"{info['channels']}ch" if info['channels'] else "?"
            dur_txt = self._fmt_duration(info["duration"])

            # DSD specific display
            is_dop = self.engine._is_dop
            is_dsd_native = self.engine._is_dsd_native
            is_dsd = is_dop or is_dsd_native
            dsd_info_line = ""
            dsd_detail = ""
            if is_dsd:
                dsd_rate = info.get("dsd_rate", sr)
                dsd_type = f"DSD{dsd_rate // 44100 // 64 * 64}" if dsd_rate >= 2822400 else "DSD"
                if is_dsd_native:
                    dsd_info_line = f"DSD Native {dsd_type}  |  {dsd_rate:,} Hz  |  {ch_txt}  |  {dur_txt}"
                    self._mode_label.config(text="DSD Native (Lossless)")
                    dsd_detail = (
                        f"DSD Rate: {dsd_rate:,} Hz\n"
                        f"DSD Type: {dsd_type}\n"
                        "Mode:     NATIVE DSD (1-bit stream)\n"
                        "Requires: ASIO + DSD-capable DAC\n"
                    )
                else:
                    dop_carrier = info.get("dop_carrier", self.engine._processed_sr)
                    dsd_info_line = f"DoP {dsd_type}  |  {dsd_rate:,} Hz DSD  |  {ch_txt}  |  {dur_txt}"
                    self._mode_label.config(text="DoP (DSD Lossless)")
                    dsd_detail = (
                        f"DSD Rate: {dsd_rate:,} Hz\n"
                        f"DoP Rate: {dop_carrier:,} Hz carrier\n"
                        f"DSD Type: {dsd_type}\n"
                        "Mode:     LOSSLESS (no conversion)\n"
                    )
                self.track_info.set(dsd_info_line)
            else:
                self.track_info.set(
                    f"{info['format']}  |  {sr_txt}  |  {ch_txt}  |  {dur_txt}"
                )
                self._mode_label.config(text="Bit-Perfect" if self.engine.bit_perfect else "DSP")

            self._sr_label.config(text=f"Output: {self.engine._processed_sr:,} Hz")
            # Update format info panel
            self.fmt_text.config(state=tk.NORMAL)
            self.fmt_text.delete("1.0", tk.END)

            dsd_opt = ""
            if is_dsd_native:
                dsd_opt = "DSD:      Native 1-bit Output\n"
            elif is_dop:
                dsd_opt = "DSD:      DoP Lossless Output\n"

            self.fmt_text.insert(tk.END,
                f"File:     {Path(filepath).name}\n"
                f"Format:   {info['format']}\n"
                + dsd_detail +
                f"Channels: {ch_txt}\n"
                f"Duration: {dur_txt}\n"
                f"Samples:  {info['samples']:,}\n"
                f"Output:   {self.engine._processed_sr:,} Hz\n"
                + (
                    "\n-- Audio Optimizations --\n"
                    "Memory:   Locked (no page faults)\n"
                    "Thread:   MMCSS Pro Audio\n"
                    "Volume:   Smooth ramp (no clicks)\n"
                    + dsd_opt
                    + "\n-- Output Mode (Mac) --\n"
                    "#1 CoreAudio       Bit-Perfect\n"
                    if IS_MACOS else
                    "\n-- Audio Optimizations --\n"
                    "Memory:   Locked (no page faults)\n"
                    "Thread:   MMCSS Pro Audio\n"
                    "Process:  High Priority\n"
                    "Power:    Ultimate Performance\n"
                    "Volume:   Smooth ramp (no clicks)\n"
                    + dsd_opt
                    + "\n-- DSD Output Mode (PC) --\n"
                    "#0 ASIO DSD Native 1-bit Lossless\n"
                    "#0 ASIO+DoP         DSD Lossless\n"
                    "#0 WASAPI+DoP       DSD Lossless\n"
                    "\n-- PCM Output Mode (PC) --\n"
                    "#1 ASIO            Bit-Perfect\n"
                    "#2 WASAPI Excl.    Bit-Perfect\n"
                    "#3 WASAPI Shared   System Resampled\n"
                    "#4 Resampled       Lossy\n"
                )
            )
            self.fmt_text.config(state=tk.DISABLED)
            self.status_var.set(f"Loaded: {Path(filepath).name}")
            self._refresh_playlist_box()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load:\n{e}")
            self.status_var.set("Load error")
        self._loading = False

    # ---- Transport ----

    def _play(self):
        # If a track is selected in the listbox and it's different from current, switch to it
        sel = self.playlist_box.curselection()
        if sel and sel[0] != self.playlist.current_index():
            self.playlist.set_current(sel[0])
            self._load_current()

        if self.engine._processed is None:
            if not self.playlist.is_empty():
                if self.playlist.current_index() < 0:
                    self.playlist.set_current(0)
                self._load_current()
            else:
                messagebox.showinfo("Info", "Add audio files to the playlist first.")
                return
        dev = self._get_device_id()
        try:
            self.engine.play(device=dev, callback=self._on_playback_end)
            self.play_btn.config(text="\u23F8  Pause")
            mode_info = self._get_mode_display()
            self.status_var.set(f"Playing  |  {self.engine._processed_sr:,} Hz  |  {mode_info}")
            self._mode_label.config(text=mode_info)
        except Exception as e:
            messagebox.showerror("Error", f"Playback failed:\n{e}")

    @staticmethod
    def _get_mode_rank(mode: str) -> str:
        """Return quality ranking label for output mode."""
        ranks = {
            "ASIO DSD Native": "#0 ASIO DSD Native (Lossless)",
            "ASIO + DoP": "#0 ASIO + DoP (DSD Lossless)",
            "WASAPI Excl. + DoP": "#0 WASAPI Excl. + DoP (DSD Lossless)",
            "ASIO": "#1 ASIO (Bit-Perfect)",
            "WASAPI Exclusive": "#2 WASAPI Exclusive (Bit-Perfect)",
            "WASAPI Shared": "#3 WASAPI Shared",
            "Resampled": "#4 Resampled (Lossy)",
            "CoreAudio": "CoreAudio (Bit-Perfect)",
            "CoreAudio (Resampled)": "CoreAudio (Resampled)",
        }
        return ranks.get(mode, mode)

    def _get_mode_display(self) -> str:
        mode = self.engine._output_mode
        return self._get_mode_rank(mode)

    def _pause(self):
        self.engine.toggle_pause()
        if self.engine.is_paused():
            self.play_btn.config(text="\u25B6  Play")
            self.status_var.set("Paused")
        else:
            self.play_btn.config(text="\u23F8  Pause")
            self.status_var.set("Playing")

    def _toggle_pause(self):
        if self.engine._processed is None:
            self._play()
        else:
            self._pause()

    def _stop(self):
        self.engine.stop()
        self.play_btn.config(text="\u25B6  Play")
        self.status_var.set("Stopped")

    def _prev_track(self):
        track = self.playlist.prev()
        if track:
            self._load_file(track)
            self._play()

    def _next_track(self):
        track = self.playlist.next()
        if track:
            self._load_file(track)
            self._play()

    def _on_playback_end(self):
        self._handle_track_end()

    def _handle_track_end(self):
        self.play_btn.config(text="\u25B6  Play")
        # Auto-play next
        track = self.playlist.next()
        if track:
            self._load_file(track)
            self._play()
        else:
            self.status_var.set("Playlist complete")

    def _on_seek(self, value):
        if not self._loading:
            try:
                self.engine.seek(float(value) / 1000.0)
            except Exception:
                pass

    def _on_volume(self, value):
        vol = float(value)
        self.engine.set_volume(vol / 100.0)
        if self.engine._is_dop or self.engine._is_dsd_native:
            self.vol_label.config(text="DoP", fg=self.PURPLE)
            self.vol_icon.config(text="\U0001F50A")
            self.status_var.set("DoP mode: volume controlled by DAC/hardware")
            return
        self.vol_label.config(text=f"{int(vol)}%")
        if vol == 0:
            self.vol_icon.config(text="\U0001F507")
            self.vol_label.config(fg=self.RED)
        elif vol < 30:
            self.vol_icon.config(text="\U0001F508")
            self.vol_label.config(fg=self.ORANGE)
        elif vol < 70:
            self.vol_icon.config(text="\U0001F509")
            self.vol_label.config(fg=self.GREEN)
        else:
            self.vol_icon.config(text="\U0001F50A")
            self.vol_label.config(fg=self.GREEN)

    def _adjust_volume(self, delta):
        vol = max(0, min(100, self.volume_var.get() + delta))
        self.volume_var.set(vol)
        self._on_volume(vol)

    # ---- DSP ----

    def _on_dsp_change(self, event=None):
        self.engine.set_bit_perfect(self.bit_perfect_var.get())
        self._apply_dsd_mode()
        os_text = self.oversample_var.get()
        factor = 1
        for f in [8, 4, 2]:
            if f"{f}x" in os_text:
                factor = f; break
        self.engine.set_oversample(factor)
        d_text = self.dither_var.get()
        if d_text == "None": self.engine.set_dither("none")
        elif d_text == "TPDF": self.engine.set_dither("tpdf", int(self.dither_bits_var.get()))
        elif d_text == "Noise-Shaped": self.engine.set_dither("noise_shaped", int(self.dither_bits_var.get()))
        mode_text = "Bit-Perfect" if self.engine.bit_perfect else f"DSP: {factor}x | {d_text} | {self.engine._processed_sr:,} Hz"
        self._sr_label.config(text=f"Output: {self.engine._processed_sr:,} Hz")
        self._mode_label.config(text="Bit-Perfect" if self.engine.bit_perfect else "DSP")
        self.status_var.set(f"DSP settings applied  |  {mode_text}")
        # Flash the status bar green to confirm
        self._flash_status()

    def _flash_status(self):
        """Flash status bar green briefly to confirm action."""
        self._status_label.config(fg=self.GREEN)
        self.root.after(1200, lambda: self._status_label.config(fg=self.FG2))

    # ---- Devices ----

    def _detect_best_dsd_mode(self) -> str:
        """Auto-detect best DSD mode based on current audio device.
        Priority: DSD Native (ASIO+DSD DAC) > DoP (universal) > DSD→PCM (fallback)"""
        device_name = self.device_var.get()
        dev_id, is_asio = self._device_map.get(device_name, (None, False))

        if is_asio:
            # Native ASIO device — check if it's a DSD-capable DAC
            # Known DSD-capable ASIO devices
            name_upper = device_name.upper()
            dsd_keywords = ["XMOS", "XCORE", "SHANLING", "IFI", "CHORD", "DENAFRIPS",
                            "RME", "LYRE", "SONNET", "GUSTARD", "TOPPING", "SMSL",
                            "FIIO", "AUNE", "HOLO", "MALAN", "EARMEN", "DSD"]
            for kw in dsd_keywords:
                if kw in name_upper:
                    return "DSD Native (ASIO)"
            # Generic ASIO — try DoP (safer than native)
            return "DoP (Lossless)"
        elif "[WASAPI]" in device_name:
            # WASAPI can do DoP but not DSD Native
            return "DoP (Lossless)"
        else:
            # Other APIs — fallback to DSD→PCM
            return "DSD\u2192PCM (Lossy)"

    def _refresh_devices(self):
        devices = AudioEngine.get_output_devices()
        names = []
        self._device_map = {}
        for dev_id, name, is_asio in devices:
            # Show API type to distinguish duplicate device names
            try:
                dev_info = sd.query_devices(dev_id)
                api_name = sd.query_hostapis(dev_info["hostapi"])["name"]
            except Exception:
                api_name = ""
            # Short API labels (cross-platform)
            api_short = {
                "Windows WASAPI": "WASAPI",
                "Windows DirectSound": "DS",
                "MME": "MME",
                "Windows WDM-KS": "WDM-KS",
                "ASIO": "ASIO",
                "Core Audio": "CoreAudio",
            }.get(api_name, api_name)

            if is_asio:
                display = f"[ASIO] {name}"
            else:
                display = f"[{api_short}] {name}"
            names.append(display)
            self._device_map[display] = (dev_id, is_asio)

        self.device_combo["values"] = names

        if IS_MACOS:
            # macOS: auto-select USB DAC or built-in output
            for n in names:
                if "[CoreAudio]" in n and ("XMOS" in n or "xCORE" in n or "Shanling" in n
                                           or "DAC" in n or "HiFi" in n or "Audio" in n):
                    self.device_var.set(n)
                    self.dev_info_var.set("CoreAudio - USB DAC detected")
                    return
            for n in names:
                if "[CoreAudio]" in n:
                    self.device_var.set(n)
                    self.dev_info_var.set("CoreAudio - Bit-perfect output")
                    return
        else:
            # Windows: native ASIO first, then WASAPI, then ASIO4ALL
            for n in names:
                if "[ASIO]" in n and "ASIO4ALL" not in n:
                    self.device_var.set(n)
                    self.dev_info_var.set("#1 ASIO - Native hardware, bit-perfect")
                    return
            for n in names:
                if "[WASAPI]" in n and ("XMOS" in n or "xCORE" in n or "Shanling" in n):
                    self.device_var.set(n)
                    self.dev_info_var.set("#2 WASAPI Exclusive - USB DAC, bit-perfect")
                    return
            for n in names:
                if "[WASAPI]" in n and ("耳机" in n or "Headphone" in n):
                    self.device_var.set(n)
                    self.dev_info_var.set("#2 WASAPI Exclusive - Bit-Perfect")
                    return
            for n in names:
                if "[WASAPI]" in n:
                    self.device_var.set(n)
                    self.dev_info_var.set("#2 WASAPI Exclusive - Bit-Perfect")
                    return
            for n in names:
                if "[ASIO]" in n:
                    self.device_var.set(n)
                    self.dev_info_var.set("#1 ASIO - May need configuration")
                    return
        if names:
            self.device_var.set(names[0])
            self.dev_info_var.set("Select a device to start")

        # Auto-detect best DSD mode based on selected device
        best_dsd = self._detect_best_dsd_mode()
        self.dsd_mode_var.set(best_dsd)
        self._apply_dsd_mode()

    def _apply_dsd_mode(self):
        """Apply current DSD mode selection to engine."""
        dsd_text = self.dsd_mode_var.get()
        if "DoP" in dsd_text:
            self.engine.dsd_mode = "dop"
        elif "Native" in dsd_text:
            self.engine.dsd_mode = "native"
        else:
            self.engine.dsd_mode = "pcm"

    def _on_device_changed(self, event=None):
        """When user switches device, auto-detect best DSD mode."""
        best_dsd = self._detect_best_dsd_mode()
        self.dsd_mode_var.set(best_dsd)
        self._apply_dsd_mode()
        # Update device info label
        name = self.device_var.get()
        dev_id, is_asio = self._device_map.get(name, (None, False))
        if is_asio:
            self.dev_info_var.set("#1 ASIO - Bit-perfect / DSD capable")
        elif "[WASAPI]" in name:
            self.dev_info_var.set("#2 WASAPI Exclusive - Bit-Perfect")
        else:
            self.dev_info_var.set("Select device for best quality")

    def _get_device_id(self):
        name = self.device_var.get()
        if name in self._device_map:
            return self._device_map[name][0]
        return None

    # ---- Timer ----

    def _update_timer(self):
        cur, tot = self.engine.get_position()
        self.time_display.set(f"{self._fmt(cur)} / {self._fmt(tot)}")
        if tot > 0 and self.engine.is_playing() and not self._loading:
            self.position_var.set((cur / tot) * 1000)
        # Poll track-end flag (avoids Thread allocation in audio callback)
        if self.engine._track_end_flag:
            self.engine._track_end_flag = False
            if self.engine._on_end:
                self.engine._on_end()
        self.root.after(100, self._update_timer)

    @staticmethod
    def _fmt(s: float) -> str:
        m, s = int(s // 60), int(s % 60)
        return f"{m:02d}:{s:02d}"

    @staticmethod
    def _fmt_duration(s: float) -> str:
        h = int(s // 3600)
        m = int(s % 3600 // 60)
        sec = int(s % 60)
        if h > 0:
            return f"{h}:{m:02d}:{sec:02d}"
        return f"{m:02d}:{sec:02d}"

    def run(self):
        self.root.mainloop()


def main():
    app = HiFiPlayerGUI()
    app.run()


if __name__ == "__main__":
    main()
