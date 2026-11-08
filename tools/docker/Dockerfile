# Cross-compilation image for the MPC plugin SDK (--toolchain docker).
#
# Contains the GNU cross compilers for both MPC CPU architectures:
#   arm-linux-gnueabihf-g++   -> 32-bit ARM  (MPC Live / X / One, "arm32")
#   aarch64-linux-gnu-g++     -> 64-bit ARM  (MPC Live II / One+ / Key 61 / X SE..., "arm64")
# plus python3 so the SDK's own tools can run inside the container if needed.
#
# Build it yourself with:
#   docker build -t mpc-plugin-sdk-cross tools/docker
# (tools/build.py --toolchain docker does this automatically the first time.)
FROM debian:bookworm

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        g++-arm-linux-gnueabihf \
        g++-aarch64-linux-gnu \
        binutils-arm-linux-gnueabihf \
        binutils-aarch64-linux-gnu \
        python3 \
        make \
        file \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# The SDK root is mounted here by build.py
WORKDIR /sdk
