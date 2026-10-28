"""Default plot of the NeXus file for counts: the Pilatus image instead of a counter (xeuss20, user's request 2026-10-23).

The BLISS writer makes the entry's default NXdata "plotselect" from scan_info["display_extra"]["displayed_channels"]
(else "plotselect", the counter selected in Flint), read when the scan is finalized. For a ct / sct (scan type "ct") with
a Pilatus image that is a single number against time; ImagePlotPreset puts the SAXS image there when such a scan is
prepared, so the file opens on the image. Other scans (loopscan, ascan, dscan, ...) are not changed.
It must be done in prepare: blissdata refuses a scan_info change between the states STARTED and STOPPED ("Scan info
changed between states STARTED and STOPPED" - the scan fails); prepare runs before scan_info is published.

    nexus_image_default(True / False)   switch on / off;  nexus_image_default()   show
"""

from bliss.scanning.chain import ChainPreset

PRESET_NAME = "xeuss_nexus_image_default"


def _image_channel(chain):
    """Full name of the SAXS Pilatus image channel of this scan (the first Pilatus if no SAXS), or None."""
    from xeuss_bliss.pilatus_camserver import PilatusCamserver

    names = []
    for node in chain.nodes_list:
        device = getattr(node, "device", None)
        if isinstance(device, PilatusCamserver):
            names.append(f"{device.name}:{device._names['image']}")
    saxs = [n for n in names if n.split(":")[-1].startswith("saxs")]
    return (saxs or names or [None])[0]


class ImagePlotPreset(ChainPreset):
    def prepare(self, chain):
        try:
            scan = chain.scan
            info = scan.scan_info
            if info.get("type") != "ct":
                return
            channel = _image_channel(chain)
            if channel is None:
                return
            display = dict(info.get("display_extra") or {})
            display["displayed_channels"] = [channel]
            info["display_extra"] = display
        except Exception:
            pass  # never disturb the scan; the writer then uses its usual default


def nexus_image_default(on=None):
    """Default plot of ct/sct entries in the data file = Pilatus image: nexus_image_default(True/False); () shows."""
    from bliss.common.scans import DEFAULT_CHAIN

    active = PRESET_NAME in DEFAULT_CHAIN._presets
    if on is not None:
        if on and not active:
            DEFAULT_CHAIN.add_preset(ImagePlotPreset(), name=PRESET_NAME)
        elif not on and active:
            DEFAULT_CHAIN.remove_preset(name=PRESET_NAME)
        active = bool(on)
    print(f"data file default plot of ct/sct = Pilatus image: {'ON' if active else 'OFF'}")
    return active
