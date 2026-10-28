# xeuss-bliss

BLISS site package of the **Xenocs Xeuss 2.0** SAXS/GISAXS instrument at xeuss20 (GeniX3D generator,
two Dectris PILATUS3 300K detectors via camserver, IMS/Schneider MDrive motors, NI PCIe-6320 counters
and shutter). It replaces SPEC on this instrument; several modules are taken over from
[xrd3003_bliss](https://github.com/lapkindm/xrd3003_bliss).

| Module | What it does |
|---|---|
| `mdrive` | motor controller for the MDrive motors |
| `ni63xx`, `acq_utils` | counters of the NI PCIe-6320 (NI-DAQmx) |
| `ni_shutter` | X-ray shutter on a digital output line of the NI card |
| `pilatus_camserver` | the PILATUS3 300K detectors through camserver |
| `genix` | GeniX3D generator status over Modbus/TCP (read-only) |
| `shutter_preset` | opens the shutter for every scan, closes it at the end (also after Ctrl-C), beam guard |
| `beamstop`, `filter`, `slits` | beamstop, attenuator and slit commands ported from the Xenocs SPEC macros |
| `scan_commands` | analysis of the last scan: `deriv`, `deriv_fit`, `deriv_fwhm`, `step_fit`, `goto_step`, `shad_fit` |
| `actions` | `with_actions`: run commands at given points of a scan |
| `saving`, `save_ct` | `newfile` with spec-like scan numbers; `save_ct` saves a finished `ct` afterwards |
| `nexus_default` | the Pilatus image as default plot of `ct`/`sct` entries |
| `notify` | e-mail when a long measurement finishes or stops on an error |
| `flint_*` | Flint start (posix_spawn instead of fork), fixes and display helpers |

[bliss_config/](bliss_config) is a copy of the instrument's Beacon configuration
(`~/bliss/beamline_configuration`, session `xeuss`), refreshed with `./sync_config.sh` before a commit.

Install with `pip install -e .` in the BLISS environment (BLISS 2.4).
