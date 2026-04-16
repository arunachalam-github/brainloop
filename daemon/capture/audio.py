"""
brainloop.capture.audio
~~~~~~~~~~~~~~~~~~~~~~~
CoreAudio property listeners for audio playback and microphone detection.

Uses ctypes to bridge the CoreAudio C API — PyObjC does not wrap CoreAudio.

Two layers of listeners:
  1. System-level: fires when the default input/output device changes
     (user plugs in headphones, switches to Bluetooth, etc.)
  2. Device-level: fires when the current default device starts/stops running
     (audio playback begins/ends, mic opens/closes)

When the default device changes, we automatically re-register the device-level
listener on the new device — so device switching is handled transparently.

Writes these triggers to the DB:
  audio_start / audio_stop  — output device started / stopped
  mic_start   / mic_stop    — input device started / stopped

Controlled by DETECT_AUDIO in config.py. If False, setup() is a no-op.
"""

import ctypes
import ctypes.util
import logging
import struct

log = logging.getLogger("brainloop.capture.audio")

# ── CoreAudio C library ───────────────────────────────────────────────────────

_ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))


# ── FourCC helper & constants ─────────────────────────────────────────────────

def _fourcc(s: str) -> int:
    return struct.unpack(">I", s.encode())[0]


kAudioObjectSystemObject                     = 1
kAudioObjectPropertyScopeGlobal              = _fourcc("glob")
kAudioObjectPropertyElementMain              = 0
kAudioHardwarePropertyDefaultOutputDevice    = _fourcc("dOut")
kAudioHardwarePropertyDefaultInputDevice     = _fourcc("dIn ")
kAudioDevicePropertyDeviceIsRunning          = _fourcc("goin")
kAudioDevicePropertyDeviceIsRunningSomewhere = _fourcc("gone")
kAudioDevicePropertyDeviceName               = _fourcc("name")


# ── Property address struct ───────────────────────────────────────────────────

class _AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope",    ctypes.c_uint32),
        ("mElement",  ctypes.c_uint32),
    ]


# ── Callback type ─────────────────────────────────────────────────────────────

_AudioListenerProc = ctypes.CFUNCTYPE(
    ctypes.c_int32,    # OSStatus return
    ctypes.c_uint32,   # AudioObjectID inObjectID
    ctypes.c_uint32,   # UInt32 inNumberAddresses
    ctypes.c_void_p,   # const AudioObjectPropertyAddress* inAddresses
    ctypes.c_void_p,   # void* inClientData
)


# ── Module-level state ────────────────────────────────────────────────────────

_out_device_id: int = 0
_in_device_id:  int = 0


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _prop_addr(selector: int) -> _AudioObjectPropertyAddress:
    return _AudioObjectPropertyAddress(
        selector,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain,
    )


def _get_uint32(obj_id: int, selector: int) -> int | None:
    """Read a single UInt32 property from a CoreAudio object. Returns None on error."""
    addr = _prop_addr(selector)
    val  = ctypes.c_uint32(0)
    size = ctypes.c_uint32(ctypes.sizeof(val))
    err  = _ca.AudioObjectGetPropertyData(
        ctypes.c_uint32(obj_id),
        ctypes.byref(addr),
        ctypes.c_uint32(0), None,
        ctypes.byref(size),
        ctypes.byref(val),
    )
    return val.value if err == 0 else None


def _add_listener(obj_id: int, selector: int, cb) -> int:
    addr = _prop_addr(selector)
    return _ca.AudioObjectAddPropertyListener(
        ctypes.c_uint32(obj_id), ctypes.byref(addr), cb, None
    )


def _remove_listener(obj_id: int, selector: int, cb) -> None:
    addr = _prop_addr(selector)
    _ca.AudioObjectRemovePropertyListener(
        ctypes.c_uint32(obj_id), ctypes.byref(addr), cb, None
    )


# ── Device-level callbacks ────────────────────────────────────────────────────
# Must be at module level and held in globals to prevent GC.

@_AudioListenerProc
def _output_running_cb(obj_id, n, addrs, ctx):
    """Fires when the default output device starts or stops playing audio."""
    from .. import db as _db_module
    running = _get_uint32(obj_id, kAudioDevicePropertyDeviceIsRunning)
    if running == 1:
        _db_module.write_snapshot("audio_start")
        log.debug("audio_start (device %d)", obj_id)
    elif running == 0:
        _db_module.write_snapshot("audio_stop")
        log.debug("audio_stop (device %d)", obj_id)
    return 0


@_AudioListenerProc
def _input_running_cb(obj_id, n, addrs, ctx):
    """Fires when the default input device starts or stops capturing (mic)."""
    from .. import db as _db_module
    running = _get_uint32(obj_id, kAudioDevicePropertyDeviceIsRunningSomewhere)
    if running == 1:
        _db_module.write_snapshot("mic_start")
        log.debug("mic_start (device %d)", obj_id)
    elif running == 0:
        _db_module.write_snapshot("mic_stop")
        log.debug("mic_stop (device %d)", obj_id)
    return 0


# ── System-level callbacks (device switching) ─────────────────────────────────

@_AudioListenerProc
def _default_output_changed_cb(obj_id, n, addrs, ctx):
    """Fires when macOS switches the default output device."""
    new_id = _get_uint32(kAudioObjectSystemObject, kAudioHardwarePropertyDefaultOutputDevice)
    if new_id:
        _reregister_output(new_id)
    return 0


@_AudioListenerProc
def _default_input_changed_cb(obj_id, n, addrs, ctx):
    """Fires when macOS switches the default input device."""
    new_id = _get_uint32(kAudioObjectSystemObject, kAudioHardwarePropertyDefaultInputDevice)
    if new_id:
        _reregister_input(new_id)
    return 0


# ── Register / re-register helpers ────────────────────────────────────────────

def _reregister_output(new_device_id: int) -> None:
    global _out_device_id
    if _out_device_id and _out_device_id != new_device_id:
        _remove_listener(_out_device_id, kAudioDevicePropertyDeviceIsRunning, _output_running_cb)
    _add_listener(new_device_id, kAudioDevicePropertyDeviceIsRunning, _output_running_cb)
    _out_device_id = new_device_id
    log.debug("Output listener → device %d", new_device_id)


def _reregister_input(new_device_id: int) -> None:
    global _in_device_id
    if _in_device_id and _in_device_id != new_device_id:
        _remove_listener(_in_device_id, kAudioDevicePropertyDeviceIsRunning, _input_running_cb)
    _add_listener(new_device_id, kAudioDevicePropertyDeviceIsRunning, _input_running_cb)
    _in_device_id = new_device_id
    log.debug("Input listener → device %d", new_device_id)


# ── Public API ────────────────────────────────────────────────────────────────

def setup() -> None:
    """
    Register all CoreAudio listeners.
    Call once from daemon.main() after the CFRunLoop is set up.
    Listeners fire on the CFRunLoop automatically — no extra threads needed.
    """
    from ..config import DETECT_AUDIO
    if not DETECT_AUDIO:
        log.info("CoreAudio detection disabled (DETECT_AUDIO=False)")
        return

    # System-level: track when the default device changes (headphones, BT, etc.)
    _add_listener(kAudioObjectSystemObject, kAudioHardwarePropertyDefaultOutputDevice, _default_output_changed_cb)
    _add_listener(kAudioObjectSystemObject, kAudioHardwarePropertyDefaultInputDevice,  _default_input_changed_cb)

    # Device-level: track running state on current default devices
    out_id = _get_uint32(kAudioObjectSystemObject, kAudioHardwarePropertyDefaultOutputDevice)
    in_id  = _get_uint32(kAudioObjectSystemObject, kAudioHardwarePropertyDefaultInputDevice)
    if out_id:
        _reregister_output(out_id)
    if in_id:
        _reregister_input(in_id)

    log.info("CoreAudio listeners active (output=%s, input=%s)", out_id, in_id)


def teardown() -> None:
    """Remove all CoreAudio listeners. Call from daemon shutdown."""
    from ..config import DETECT_AUDIO
    if not DETECT_AUDIO:
        return

    _remove_listener(kAudioObjectSystemObject, kAudioHardwarePropertyDefaultOutputDevice, _default_output_changed_cb)
    _remove_listener(kAudioObjectSystemObject, kAudioHardwarePropertyDefaultInputDevice,  _default_input_changed_cb)
    if _out_device_id:
        _remove_listener(_out_device_id, kAudioDevicePropertyDeviceIsRunning, _output_running_cb)
    if _in_device_id:
        _remove_listener(_in_device_id,  kAudioDevicePropertyDeviceIsRunning, _input_running_cb)

    log.info("CoreAudio listeners removed")


def _get_device_name(device_id: int) -> str | None:
    """Read the human-readable name of a CoreAudio device."""
    addr = _prop_addr(kAudioDevicePropertyDeviceName)
    buf  = ctypes.create_string_buffer(256)
    size = ctypes.c_uint32(256)
    err  = _ca.AudioObjectGetPropertyData(
        ctypes.c_uint32(device_id),
        ctypes.byref(addr),
        ctypes.c_uint32(0), None,
        ctypes.byref(size),
        buf,
    )
    return buf.value.decode("utf-8", errors="replace") if err == 0 else None


def audio_playing() -> bool:
    """Return True if any process is currently outputting audio to the default output device."""
    if not _out_device_id:
        return False
    return _get_uint32(_out_device_id, kAudioDevicePropertyDeviceIsRunningSomewhere) == 1


def mic_active() -> bool:
    """Return True if any process is currently capturing from the default input device (mic open)."""
    if not _in_device_id:
        return False
    return _get_uint32(_in_device_id, kAudioDevicePropertyDeviceIsRunningSomewhere) == 1


def output_device_name() -> str | None:
    """Return the name of the current default output device, or None if unavailable."""
    if not _out_device_id:
        return None
    return _get_device_name(_out_device_id)


def input_device_name() -> str | None:
    """Return the name of the current default input device, or None if unavailable."""
    if not _in_device_id:
        return None
    return _get_device_name(_in_device_id)
