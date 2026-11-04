"""
An identity's captured camera and microphone report what a real device does.

Stock Firefox 158 on win-i9 (cfparity perms.granted, a 720p laptop camera and a
microphone array) reports:
  video getCapabilities  width 2..1280, height 2..720, frameRate 0..30, no
                         facingMode; getSettings 640x480 at 30, no facingMode
  audio getCapabilities  echoCancellation / autoGainControl / noiseSuppression
                         [true, false], channelCount 1..2; getSettings all
                         three true, channelCount 2
and a microphone's settings follow the page's constraints (AGC and NS ride on
AEC: MediaEngineWebRTCMicrophoneSource::EvaluateSettings). Camoufox serves the
identity's devices through the fake engine, which answered the test device's
4096x2160 "environment" camera and a processing-less mono microphone
(MediaEngineFake.cpp, media-device-spoofing.patch; LEAKS #103).

The per-device maxima and channel counts come from mediaDevices:webcamMax* and
mediaDevices:microphoneChannels (drawn by the launcher from media-devices.json);
a Windows camera labelled "Front"/"Rear" has the facing mode stock's
GetFacingMode gives it.

    python browser/tests/playwright/patches/media-track-capabilities-identity.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import launch_kwargs  # noqa: E402

CONFIG = {
    "mediaDevices:enabled": True,
    "mediaDevices:micros": 2,
    "mediaDevices:webcams": 2,
    "mediaDevices:speakers": 1,
    "mediaDevices:microphoneLabels": ["Microphone Array (Realtek(R) Audio)",
                                      "Microphone (Logitech HD Webcam C270)"],
    "mediaDevices:microphoneGroups": ["hw-1", "hw-3"],
    "mediaDevices:microphoneChannels": [2, 1],
    "mediaDevices:webcamLabels": ["Integrated Camera", "Microsoft Camera Front"],
    "mediaDevices:webcamGroups": ["hw-2", "hw-3"],
    "mediaDevices:webcamMaxWidths": [1280, 1920],
    "mediaDevices:webcamMaxHeights": [720, 1080],
    "mediaDevices:webcamMaxFrameRates": [30, 30],
    "mediaDevices:speakerLabels": ["Speakers (Realtek(R) Audio)"],
    "mediaDevices:speakerGroups": ["hw-1"],
}

PROBE = """async () => {
  const pick = (o, keys) => Object.fromEntries(keys.filter((k) => k in o).map((k) => [k, o[k]]));
  const VK = ['width', 'height', 'frameRate', 'facingMode', 'resizeMode'];
  const AK = ['echoCancellation', 'autoGainControl', 'noiseSuppression', 'channelCount'];
  const one = async (c, keys) => {
    try {
      const s = await navigator.mediaDevices.getUserMedia(c);
      const t = s.getTracks()[0];
      const r = {label: t.label, caps: pick(t.getCapabilities(), keys), settings: pick(t.getSettings(), keys)};
      s.getTracks().forEach((x) => x.stop());
      return r;
    } catch (e) { return {error: e.name, constraint: e.constraint}; }
  };
  // enumerateDevices() exposes a kind's ids and labels only once that kind
  // was captured (MediaDevices::CanExposeInfo, as stock): ask after each.
  // An unknown label throws: {exact: undefined} would be no constraint, and
  // the default device would stand in for the one asked for.
  const idOf = async (label) => {
    const d = (await navigator.mediaDevices.enumerateDevices()).find((x) => x.label === label);
    if (!d) throw new Error(`no device labelled ${label}`);
    return d.deviceId;
  };
  const out = {};
  out.cam = await one({video: true}, VK);
  out.front = await one({video: {deviceId: {exact: await idOf('Microsoft Camera Front')}}}, VK);
  out.tooWide = await one({video: {deviceId: {exact: await idOf('Integrated Camera')}, width: {min: 1920}}}, VK);
  out.mic = await one({audio: true}, AK);
  const realtek = await idOf('Microphone Array (Realtek(R) Audio)');
  out.micNoAec = await one({audio: {deviceId: {exact: realtek}, echoCancellation: false}}, AK);
  out.micMono = await one({audio: {deviceId: {exact: realtek}, channelCount: {exact: 1}}}, AK);
  out.monoDevice = await one({audio: {deviceId: {exact: await idOf('Microphone (Logitech HD Webcam C270)')}}}, AK);
  return out;
}"""

EXPECT = {
    "cam": {"caps": {"width": {"max": 1280, "min": 2}, "height": {"max": 720, "min": 2},
                     "frameRate": {"max": 30, "min": 0}},
            "settings": {"width": 640, "height": 480, "frameRate": 30}},
    "front": {"caps": {"width": {"max": 1920, "min": 2}, "height": {"max": 1080, "min": 2},
                       "facingMode": ["user"]},
              "settings": {"facingMode": "user"}},
    "mic": {"label": "Microphone Array (Realtek(R) Audio)", "caps": {"echoCancellation": [True, False], "autoGainControl": [True, False],
                     "noiseSuppression": [True, False], "channelCount": {"max": 2, "min": 1}},
            "settings": {"echoCancellation": True, "autoGainControl": True, "noiseSuppression": True,
                         "channelCount": 2}},
    "micNoAec": {"settings": {"echoCancellation": False, "autoGainControl": False,
                              "noiseSuppression": False, "channelCount": 2}},
    "micMono": {"settings": {"channelCount": 1}},
    "monoDevice": {"label": "Microphone (Logitech HD Webcam C270)",
                   "caps": {"channelCount": {"max": 1, "min": 1}}, "settings": {"channelCount": 1}},
}


def _sub(want, got, path=""):
    """Every key of `want` equals `got`'s; returns the first mismatch."""
    if isinstance(want, dict):
        if not isinstance(got, dict):
            return f"{path}: {got!r} is not an object"
        for k, v in want.items():
            if k not in got:
                return f"{path}.{k}: missing"
            bad = _sub(v, got[k], f"{path}.{k}")
            if bad:
                return bad
        return None
    return None if want == got else f"{path}: {got!r}, stock {want!r}"


def main() -> int:
    from camoufox.sync_api import Camoufox

    with Camoufox(**launch_kwargs(os="windows", config=CONFIG, i_know_what_im_doing=True,
                                  firefox_user_prefs={"media.navigator.permission.disabled": True})) as browser:
        page = browser.new_page()
        page.goto("https://example.com/")
        got = page.evaluate(PROBE)
    for k, v in got.items():
        print(f"  {k}: {v}")
    fails = []
    for k, want in EXPECT.items():
        if "error" in got.get(k, {}):
            fails.append(f"{k}: getUserMedia failed {got[k]}")
            continue
        bad = _sub(want, got.get(k), k)
        if bad:
            fails.append(bad)
    for k in ("cam",):
        for part in ("caps", "settings"):
            if "facingMode" in got.get(k, {}).get(part, {}):
                fails.append(f"{k}.{part}.facingMode present; a Windows laptop camera has none")
    if got.get("tooWide", {}).get("error") != "OverconstrainedError":
        fails.append(f"tooWide: width min 1920 on a 1280 camera gave {got.get('tooWide')}, stock OverconstrainedError")
    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1
    print("PASS: identity camera and microphone report a real device's capabilities and settings, "
          "and the settings follow the page's constraints")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
