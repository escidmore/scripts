#!/bin/bash
set -e

PREFIX="$HOME/ffmpeg_build"
NIX_PROFILE="$HOME/.nix-profile"

mkdir -p "$PREFIX"/{bin,lib,lib64,include,share}

# Zen 4 optimized flags
export CFLAGS="-O3 -march=znver4 -mtune=znver4 -pipe -fno-plt"
export CXXFLAGS="-O3 -march=znver4 -mtune=znver4 -pipe"

export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig:$PREFIX/lib64/pkgconfig:$PREFIX/lib/x86_64-linux-gnu/pkgconfig:$NIX_PROFILE/lib/pkgconfig:$NIX_PROFILE/share/pkgconfig"
export CPATH="$PREFIX/include:$NIX_PROFILE/include"
export LIBRARY_PATH="$PREFIX/lib:$PREFIX/lib64:$NIX_PROFILE/lib"

mkdir -p ~/repo
cd ~/repo


echo "=========================================="
echo "Building SVT-AV1"
echo "=========================================="
git -C SVT-AV1 pull 2>/dev/null || git clone https://gitlab.com/AOMediaCodec/SVT-AV1.git
cd SVT-AV1
rm -rf build && mkdir build && cd build
cmake -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DBUILD_SHARED_LIBS=OFF \
    -DBUILD_DEC=OFF \
    -DNATIVE=ON \
    -DSVT_AV1_LTO=ON \
    ..
ninja -j$(nproc)
ninja install

echo "=========================================="
echo "Building dav1d"
echo "=========================================="
cd ~/repo
git -C dav1d pull 2>/dev/null || git clone --depth 1 https://code.videolan.org/videolan/dav1d.git
cd dav1d
rm -rf build
meson setup build \
    --prefix="$PREFIX" \
    --buildtype=release \
    --default-library=static \
    -Denable_tools=false \
    -Denable_tests=false \
    -Db_lto=true
ninja -C build -j$(nproc)
ninja -C build install

echo "=========================================="
echo "Building VMAF"
echo "=========================================="
cd ~/repo
git -C vmaf pull 2>/dev/null || git clone https://github.com/Netflix/vmaf.git
cd vmaf/libvmaf
rm -rf build
meson setup build \
    --prefix="$PREFIX" \
    --buildtype=release \
    --default-library=static \
    -Db_lto=true
ninja -C build -j$(nproc)
ninja -C build install

echo "=========================================="
echo "Building x264"
echo "=========================================="
cd ~/repo
git -C x264 pull 2>/dev/null || git clone --depth 1 https://code.videolan.org/videolan/x264.git
cd x264
make distclean 2>/dev/null || true
./configure \
    --prefix="$PREFIX" \
    --enable-static \
    --enable-pic \
    --disable-opencl
make -j$(nproc)
make install

echo "=========================================="
echo "Building x265"
echo "=========================================="
cd ~/repo
rm -rf multicoreware* x265.tar.bz2
wget -O x265.tar.bz2 https://bitbucket.org/multicoreware/x265_git/get/master.tar.bz2
tar xjf x265.tar.bz2
cd multicoreware*/build/linux
cmake -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DENABLE_SHARED=OFF \
    -DENABLE_CLI=OFF \
    ../../source
ninja -j$(nproc)
ninja install

echo "=========================================="
echo "Building FFmpeg w/ Zen 4 + LTO"
echo "=========================================="
cd ~/repo
rm -rf ffmpeg ffmpeg-snapshot.tar.bz2
wget -O ffmpeg-snapshot.tar.bz2 https://ffmpeg.org/releases/ffmpeg-snapshot.tar.bz2
tar xjf ffmpeg-snapshot.tar.bz2
cd ffmpeg

./configure \
    --prefix="$PREFIX" \
    --bindir="$PREFIX/bin" \
    --pkg-config-flags="--static" \
    --extra-cflags="-O3 -march=znver4 -mtune=znver4 -flto=auto -I$PREFIX/include -I$NIX_PROFILE/include" \
    --extra-ldflags="-flto=auto -L$PREFIX/lib -L$PREFIX/lib64 -L$PREFIX/lib/x86_64-linux-gnu -L$NIX_PROFILE/lib" \
    --extra-libs="-lpthread -lm -lz -ldl" \
    --ld="g++" \
    --enable-gpl \
    --enable-nonfree \
    --enable-gnutls \
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
    --enable-libdav1d \
    --enable-libvmaf \
    --enable-libass \
    --enable-libfribidi \
    --enable-libharfbuzz \
    --enable-runtime-cpudetect \
    --disable-debug \
    --disable-doc

make -j$(nproc)
make install

echo ""
echo "=========================================="
echo "Build complete!"
echo "=========================================="
echo ""
"$PREFIX/bin/ffmpeg" -version
echo ""
echo "Encoders:"
"$PREFIX/bin/ffmpeg" -encoders 2>/dev/null | grep -E '(svt|x264|x265|libvpx|opus|aac)'
echo ""
echo "Decoders:"
"$PREFIX/bin/ffmpeg" -decoders 2>/dev/null | grep -E '(dav1d|libvpx|opus|vorbis)'

