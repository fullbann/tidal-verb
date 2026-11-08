# Releasing

1. Bump `version` in `plugins/<Name>/plugin.json` only if you changed something users can hear/see; keep `uid`, `.so` name and catalog id fixed. Versions are X.Y.Z (Z fixes, Y new parameters, X only if parameter positions change).
2. Actions → *Release (draft)* → Run workflow with `dry_run` on; check the attached zips and skin previews.
3. Run again with `dry_run` off. This creates a **draft** release.
4. Install the armv7 zip on a device (`install.sh`), load it, play, check Q-Links and pages, save/reload a project, then `uninstall.sh`.
5. Publish the draft (this creates the tag).
6. Optionally add `tested.json` (`[{"version","device","firmware","date"}]`) with what you really tested.
7. Catalog entry lives in open-mpc/mpc-vst-plugins `catalog/plugins/<id>.json`; the registry stores no versions or checksums.
