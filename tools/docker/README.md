# Docker / Podman cross-compile toolchain

The default toolchain for this SDK is **zig** (no Docker needed). This folder is an
alternative for people who prefer the classic GNU cross compilers, or who cannot use zig.

```
python tools/build.py plugins/MyPlugin --toolchain docker
```

What happens:

1. `build.py` looks for `docker` on your PATH, then `podman`. Whichever it finds is used.
2. On first use it builds the image in this folder (`tools/docker/Dockerfile`, tagged
   `mpc-plugin-sdk-cross`). It is based on `debian:bookworm` and contains
   `g++-arm-linux-gnueabihf` (arm32) and `g++-aarch64-linux-gnu` (arm64). This needs
   network access once and takes a few minutes.
3. For every ARM target the SDK root is mounted into the container at `/sdk` (and your
   project at `/project` if it lives outside the SDK folder) and the cross compiler is run
   there with the same flags `--toolchain gcc` would use. The output `.so` files land in
   your project's `build/` folder exactly as with the zig toolchain.
4. The *native* build (used for `host_sim` and the parameter dump) still runs on your own
   machine with your system C++ compiler (`c++`, `g++` or `clang++`). If you have none the
   native step is skipped with a warning and the UI is generated from an older
   `build/params.json` if one exists.

Rebuild the image after editing the Dockerfile:

```
docker build -t mpc-plugin-sdk-cross tools/docker
```

Troubleshooting:

* "permission denied" on the docker socket (Linux): add your user to the `docker` group or
  use `podman`, which runs rootless.
* On Linux the container runs as your own user id so the output files are not owned by root.
* Apple Silicon Macs: the image is built for your host architecture automatically; the cross
  compilers inside it work the same on arm64 and x86_64 hosts.
