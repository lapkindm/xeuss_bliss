"""Flint entry point for xeuss20: `python -m xeuss_bliss.flint_main <flint arguments>` = `python -m flint`
plus the fixes of xeuss_bliss.flint_patches (started by xeuss_bliss.flint_spawn)."""

from xeuss_bliss import flint_patches

flint_patches.install()

from flint.flint import main  # noqa: E402

main()
