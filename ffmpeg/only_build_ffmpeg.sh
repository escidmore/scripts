#!/bin/bash
set -e

PREFIX="$HOME/ffmpeg_build"
NIX_PROFILE="$HOME/.nix-profile"
mkdir -p "$PREFIX"/{bin,lib,lib64,include,share}

export CFLAGS="-O3 -march=native -mtune=native -pipe -fno-plt"
export CXXFLAGS="-O3 -march=native -mtune=native -pipe"

export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig:$PREFIX/lib64/pkgconfig:$PREFIX/lib/x86_64-linux-gnu/pkgconfig:$HOME/.nix-profile/lib/pkgconfig:$HOME/.nix-profile/share/pkgconfig"
export CPATH="$PREFIX/include:$NIX_PROFILE/include"
export LIBRARY_PATH="$PREFIX/lib:$PREFIX/lib64:$NIX_PROFILE/lib"
export NIX_CC_WRAPPER_TARGET_HOST_x86_64_unknown_linux_gnu=1
export NIX_CFLAGS_COMPILE="-idirafter $NIX_PROFILE/include"
export NIX_LDFLAGS="-L$NIX_PROFILE/lib -rpath $NIX_PROFILE/lib"

cd ~/repo
# rm -rf ffmpeg ffmpeg-snapshot.tar.bz2
# wget -O ffmpeg-snapshot.tar.bz2 https://ffmpeg.org/releases/ffmpeg-snapshot.tar.bz2
# tar xjf ffmpeg-snapshot.tar.bz2
cd ffmpeg

./configure \
    --prefix="$PREFIX" \
    --bindir="$PREFIX/bin" \
    --extra-cflags="-O3 -march=native -mtune=native -I$PREFIX/include -I$NIX_PROFILE/include" \
    --extra-ldflags="-L$PREFIX/lib -L$PREFIX/lib64 -L$PREFIX/lib/x86_64-linux-gnu -L$NIX_PROFILE/lib" \
    --extra-libs="-lpthread -lm -lz -ldl" \
    --ld="g++" \
    --enable-gpl \
    --enable-nonfree \
    --enable-libfdk-aac \
    --enable-libfreetype \
    --enable-libfontconfig \
    --enable-libmp3lame \
    --enable-libopus \
    --enable-libvorbis \
    --enable-libvpx \
    --enable-libx264 \
    --enable-libx265 \
    --enable-libsvtav1 \
    --enable-libvmaf \
    --enable-libass \
    --enable-libfribidi \
    --enable-libharfbuzz \
    --enable-runtime-cpudetect \
    --disable-debug \
    --disable-doc

make -j$(nproc)
make
