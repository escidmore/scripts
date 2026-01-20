#!/bin/bash
set -euo pipefail
IFS=$'\n\t'

# ----------------------------
# Config
# ----------------------------
export PREFIX="$HOME/ffmpeg_build"

mkdir -p "$PREFIX"/{bin,lib,lib64,include,share,lib/pkgconfig}
mkdir -p "$HOME/repo"

# Zen 4 baseline for 7950X3D + 7945HX fleet
export BASE_CFLAGS="-O3 -march=znver4 -mtune=znver4 -pipe -fno-plt"
export BASE_CXXFLAGS="-O3 -march=znver4 -mtune=znver4 -pipe"

# Static build flags
export STATIC_CFLAGS="$BASE_CFLAGS"
export STATIC_CXXFLAGS="$BASE_CXXFLAGS"

# pkg-config should only look at our prefix (meson uses lib/x86_64-linux-gnu)
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig:$PREFIX/lib64/pkgconfig:$PREFIX/lib/x86_64-linux-gnu/pkgconfig"
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig:$PREFIX/lib64/pkgconfig:$PREFIX/lib/x86_64-linux-gnu/pkgconfig"

# Library paths for compile-time linking only
export LIBRARY_PATH="$PREFIX/lib:$PREFIX/lib64"
# DO NOT set LD_LIBRARY_PATH - let Nix tools use their rpath'd libraries
# Setting it causes libstdc++ ABI mismatches with clang

# Include path
export CPATH="$PREFIX/include"

# ----------------------------
# Toolchain: LLVM everywhere
# ----------------------------
require_tool() {
  local n="$1"
  local p
  p="$(command -v "$n" 2>/dev/null || true)"
  if [[ -z "$p" ]]; then
    echo "ERROR: missing required tool: $n" >&2
    exit 1
  fi
  echo "$p"
}

CLANG="$(require_tool clang)"
CLANGXX="$(require_tool clang++)"
LLVM_AR="$(require_tool llvm-ar)"
LLVM_RANLIB="$(require_tool llvm-ranlib)"
LLVM_NM="$(require_tool llvm-nm)"
LLVM_STRIP="$(require_tool llvm-strip)"
PKG_CONFIG_BIN="$(require_tool pkg-config)"

export CC="$CLANG"
export CXX="$CLANGXX"
export LD="$CLANG"
export AR="$LLVM_AR"
export RANLIB="$LLVM_RANLIB"
export NM="$LLVM_NM"
export STRIP="$LLVM_STRIP"

export LDFLAGS="-fuse-ld=lld -L$PREFIX/lib -L$PREFIX/lib64"
export CFLAGS="$BASE_CFLAGS -I$PREFIX/include"
export CXXFLAGS="$BASE_CXXFLAGS -I$PREFIX/include"

# Autotools needs to find libtool m4 macros
NIX_ACLOCAL="$HOME/.nix-profile/share/aclocal"
export ACLOCAL_PATH="$NIX_ACLOCAL${ACLOCAL_PATH:+:$ACLOCAL_PATH}"

# Cache directory for tarballs
CACHE_DIR="$HOME/.cache/ffmpeg_build"
mkdir -p "$CACHE_DIR"

# Track if any dependency was rebuilt
REBUILD_FFMPEG=false

# Force flags
FORCE_FFMPEG="${FORCE_FFMPEG:-0}"
FORCE_FFMPEG_CLEAN="${FORCE_FFMPEG_CLEAN:-0}"
FORCE_ALL="${FORCE_ALL:-0}"

# ----------------------------
# Helpers
# ----------------------------
require_cmd() {
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || { echo "ERROR: missing command: $c" >&2; exit 1; }
  done
}

require_cmd git curl tar cmake ninja meson nasm "$PKG_CONFIG_BIN"

git_update() {
  local url="$1"
  local dir="$2"
  local depth="${3:-1}"

  if [[ -d "$dir/.git" ]]; then
    if [[ "$FORCE_ALL" == "1" ]]; then
      echo "FORCE_ALL: rebuilding $dir"
      return 0
    fi

    git -C "$dir" fetch --prune origin
    local old_head new_head
    old_head=$(git -C "$dir" rev-parse HEAD)
    new_head=$(git -C "$dir" rev-parse origin/HEAD 2>/dev/null || \
               git -C "$dir" rev-parse origin/main 2>/dev/null || \
               git -C "$dir" rev-parse origin/master 2>/dev/null || \
               git -C "$dir" rev-parse FETCH_HEAD)

    if [[ "$old_head" != "$new_head" ]]; then
      echo "Updating $dir: $old_head -> $new_head"
      git -C "$dir" reset --hard "$new_head"
      return 0
    fi
    echo "No changes in $dir"
    return 1
  else
    if [[ "$depth" -gt 0 ]]; then
      git clone --depth "$depth" "$url" "$dir"
    else
      git clone "$url" "$dir"
    fi
    return 0
  fi
}

download_if_changed() {
  local url="$1"
  local filename="$2"
  local path="$CACHE_DIR/$filename"

  if [[ "$FORCE_ALL" == "1" && -f "$path" ]]; then
    rm -f "$path"
  fi

  if [[ -f "$path" ]]; then
    echo "Using cached: $filename"
    return 1
  else
    echo "Downloading: $filename"
    curl -fSL -o "$path" "$url"
    return 0
  fi
}

# Find GCC runtime library path (needed when using lld instead of Nix's ld wrapper)
# Capture full output first to avoid SIGPIPE from early pipe termination
# The clang command fails (linker error) but we still get the verbose output we need
_clang_verbose=$("$CLANGXX" -v /dev/null -o /dev/null 2>&1 || true)
GCC_LIB_PATH=$(echo "$_clang_verbose" | grep -oP '(?<=-L)/nix/store/[^/]+-gcc-[^/]+-lib/lib' | head -1 || true)
if [[ -z "$GCC_LIB_PATH" ]]; then
  echo "WARNING: Could not detect GCC lib path, C++ binaries may not run" >&2
  LLD_LINK_ARGS="'-fuse-ld=lld'"
else
  # Need both -L (for linking) and -rpath (for runtime) when using lld
  LLD_LINK_ARGS="'-fuse-ld=lld', '-L${GCC_LIB_PATH}', '-Wl,-rpath,${GCC_LIB_PATH}'"
  # Update global LDFLAGS to include GCC lib path for autotools builds
  export LDFLAGS="$LDFLAGS -L$GCC_LIB_PATH -Wl,-rpath,$GCC_LIB_PATH"
fi

# Meson native file for LLVM toolchain
MESON_NATIVE_FILE="$CACHE_DIR/clang-native.ini"
cat > "$MESON_NATIVE_FILE" <<EOF
[binaries]
c = '${CC}'
cpp = '${CXX}'
ar = '${AR}'
ranlib = '${RANLIB}'
nm = '${NM}'
strip = '${STRIP}'
ld = 'ld.lld'
pkg-config = '${PKG_CONFIG_BIN}'

[built-in options]
c_link_args = [${LLD_LINK_ARGS}]
cpp_link_args = [${LLD_LINK_ARGS}]
EOF

# CMake toolchain settings
CMAKE_COMMON="-G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX=$PREFIX \
  -DCMAKE_PREFIX_PATH=$PREFIX \
  -DCMAKE_C_COMPILER=$CLANG \
  -DCMAKE_CXX_COMPILER=$CLANGXX \
  -DCMAKE_AR=$LLVM_AR \
  -DCMAKE_RANLIB=$LLVM_RANLIB \
  -DCMAKE_NM=$LLVM_NM \
  -DCMAKE_STRIP=$LLVM_STRIP \
  -DCMAKE_C_FLAGS=\"$STATIC_CFLAGS\" \
  -DCMAKE_CXX_FLAGS=\"$STATIC_CXXFLAGS\" \
  -DCMAKE_EXE_LINKER_FLAGS=\"-fuse-ld=lld\" \
  -DCMAKE_SHARED_LINKER_FLAGS=\"-fuse-ld=lld\" \
  -DBUILD_SHARED_LIBS=OFF"

cd "$HOME/repo"

# ==========================================
# Build zlib (many deps need this)
# ==========================================
echo "=========================================="
echo "Building zlib"
echo "=========================================="
if git_update "https://github.com/madler/zlib.git" "zlib" 1; then
  cd "$HOME/repo/zlib"
  rm -rf build && mkdir build && cd build

  cmake -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DCMAKE_C_COMPILER="$CLANG" \
    -DCMAKE_AR="$LLVM_AR" \
    -DCMAKE_RANLIB="$LLVM_RANLIB" \
    -DCMAKE_C_FLAGS="$STATIC_CFLAGS" \
    -DZLIB_BUILD_EXAMPLES=OFF \
    ..

  ninja -j"$(nproc)"
  ninja install
  # Remove shared libraries to ensure static linking
  rm -f "$PREFIX"/lib*/libz.so*
  REBUILD_FFMPEG=true
fi

# ==========================================
# Build libogg (vorbis needs this)
# ==========================================
echo "=========================================="
echo "Building libogg"
echo "=========================================="
cd "$HOME/repo"
if git_update "https://github.com/xiph/ogg.git" "ogg" 1; then
  cd "$HOME/repo/ogg"
  rm -rf build && mkdir build && cd build

  cmake -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DCMAKE_C_COMPILER="$CLANG" \
    -DCMAKE_AR="$LLVM_AR" \
    -DCMAKE_RANLIB="$LLVM_RANLIB" \
    -DCMAKE_C_FLAGS="$STATIC_CFLAGS" \
    -DBUILD_SHARED_LIBS=OFF \
    -DBUILD_TESTING=OFF \
    ..

  ninja -j"$(nproc)"
  ninja install
  REBUILD_FFMPEG=true
fi

# ==========================================
# Build libvorbis
# ==========================================
echo "=========================================="
echo "Building libvorbis"
echo "=========================================="
cd "$HOME/repo"
if git_update "https://github.com/xiph/vorbis.git" "vorbis" 1; then
  cd "$HOME/repo/vorbis"
  rm -rf build && mkdir build && cd build

  cmake -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DCMAKE_PREFIX_PATH="$PREFIX" \
    -DCMAKE_C_COMPILER="$CLANG" \
    -DCMAKE_AR="$LLVM_AR" \
    -DCMAKE_RANLIB="$LLVM_RANLIB" \
    -DCMAKE_C_FLAGS="$STATIC_CFLAGS" \
    -DBUILD_SHARED_LIBS=OFF \
    -DBUILD_TESTING=OFF \
    ..

  ninja -j"$(nproc)"
  ninja install
  REBUILD_FFMPEG=true
fi

# ==========================================
# Build opus
# ==========================================
echo "=========================================="
echo "Building opus"
echo "=========================================="
cd "$HOME/repo"
if git_update "https://github.com/xiph/opus.git" "opus" 1; then
  cd "$HOME/repo/opus"
  rm -rf build && mkdir build && cd build

  cmake -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DCMAKE_C_COMPILER="$CLANG" \
    -DCMAKE_AR="$LLVM_AR" \
    -DCMAKE_RANLIB="$LLVM_RANLIB" \
    -DCMAKE_C_FLAGS="$STATIC_CFLAGS" \
    -DBUILD_SHARED_LIBS=OFF \
    -DOPUS_BUILD_PROGRAMS=OFF \
    -DOPUS_BUILD_TESTING=OFF \
    ..

  ninja -j"$(nproc)"
  ninja install
  REBUILD_FFMPEG=true
fi

# ==========================================
# Build lame (mp3)
# ==========================================
echo "=========================================="
echo "Building lame"
echo "=========================================="
cd "$HOME/repo"
if download_if_changed "http://downloads.sourceforge.net/project/lame/lame/3.100/lame-3.100.tar.gz" "lame-3.100.tar.gz"; then
  rm -rf "$HOME/repo/lame-3.100"
fi

if [[ ! -d "$HOME/repo/lame-3.100" ]]; then
  tar -xf "$CACHE_DIR/lame-3.100.tar.gz" -C "$HOME/repo"
  cd "$HOME/repo/lame-3.100"

  CFLAGS="$STATIC_CFLAGS" \
  ./configure \
    --prefix="$PREFIX" \
    --enable-nasm \
    --disable-shared \
    --enable-static \
    --with-pic

  make -j"$(nproc)"
  make install
  REBUILD_FFMPEG=true
fi

# ==========================================
# Build fdk-aac
# ==========================================
echo "=========================================="
echo "Building fdk-aac"
echo "=========================================="
cd "$HOME/repo"
if git_update "https://github.com/mstorsjo/fdk-aac.git" "fdk-aac" 1; then
  cd "$HOME/repo/fdk-aac"
  rm -rf build && mkdir build && cd build

  cmake -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DCMAKE_C_COMPILER="$CLANG" \
    -DCMAKE_CXX_COMPILER="$CLANGXX" \
    -DCMAKE_AR="$LLVM_AR" \
    -DCMAKE_RANLIB="$LLVM_RANLIB" \
    -DCMAKE_C_FLAGS="$STATIC_CFLAGS" \
    -DCMAKE_CXX_FLAGS="$STATIC_CXXFLAGS" \
    -DBUILD_SHARED_LIBS=OFF \
    -DFDK_AAC_BUILD_PROGRAMS=OFF \
    ..

  ninja -j"$(nproc)"
  ninja install
  REBUILD_FFMPEG=true
fi

# ==========================================
# Build libvpx
# ==========================================
echo "=========================================="
echo "Building libvpx"
echo "=========================================="
cd "$HOME/repo"
if git_update "https://chromium.googlesource.com/webm/libvpx.git" "libvpx" 1; then
  cd "$HOME/repo/libvpx"
  make clean 2>/dev/null || true

  CFLAGS="$STATIC_CFLAGS" \
  CXXFLAGS="$STATIC_CXXFLAGS" \
  ./configure \
    --prefix="$PREFIX" \
    --disable-examples \
    --disable-unit-tests \
    --enable-vp9 \
    --enable-vp8 \
    --enable-vp9-highbitdepth \
    --enable-pic \
    --enable-better-hw-compatibility \
    --enable-multi-res-encoding \
    --disable-shared \
    --enable-static

  make -j"$(nproc)"
  make install
  REBUILD_FFMPEG=true
fi

# ==========================================
# Build numactl (x265 needs this)
# ==========================================
echo "=========================================="
echo "Building numactl"
echo "=========================================="
cd "$HOME/repo"
if git_update "https://github.com/numactl/numactl.git" "numactl" 0; then
  cd "$HOME/repo/numactl"
  ./autogen.sh

  CFLAGS="$STATIC_CFLAGS" \
  ./configure \
    --prefix="$PREFIX" \
    --disable-shared \
    --enable-static

  make -j"$(nproc)"
  make install
  REBUILD_FFMPEG=true
fi

# ==========================================
# Build x264
# ==========================================
echo "=========================================="
echo "Building x264"
echo "=========================================="
cd "$HOME/repo"
if git_update "https://code.videolan.org/videolan/x264.git" "x264" 1; then
  cd "$HOME/repo/x264"
  make clean 2>/dev/null || true

  CFLAGS="$STATIC_CFLAGS" \
  ./configure \
    --prefix="$PREFIX" \
    --enable-static \
    --enable-pic \
    --disable-opencl

  make -j"$(nproc)"
  make install
  REBUILD_FFMPEG=true
fi

# ==========================================
# Build x265
# ==========================================
echo "=========================================="
echo "Building x265"
echo "=========================================="
cd "$HOME/repo"
if download_if_changed "https://bitbucket.org/multicoreware/x265_git/get/master.tar.bz2" "x265-master.tar.bz2"; then
  rm -rf "$HOME/repo"/multicoreware-x265_git-*
fi

# Use read to get first line - avoids SIGPIPE with pipefail
read -r x265_first_entry < <(tar -tf "$CACHE_DIR/x265-master.tar.bz2")
x265_dir="${x265_first_entry%%/*}"
if [[ ! -f "$HOME/repo/$x265_dir/build-done" ]] || [[ "$FORCE_ALL" == "1" ]]; then
  rm -rf "$HOME/repo"/multicoreware-x265_git-*
  tar -xf "$CACHE_DIR/x265-master.tar.bz2" -C "$HOME/repo"
  # Use glob expansion instead of ls|head to avoid SIGPIPE
  x265_dirs=("$HOME/repo"/multicoreware-x265_git-*)
  x265_dir="${x265_dirs[0]}"

  cd "$x265_dir"
  rm -rf build && mkdir build && cd build

  cmake -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DCMAKE_C_COMPILER="$CLANG" \
    -DCMAKE_CXX_COMPILER="$CLANGXX" \
    -DCMAKE_AR="$LLVM_AR" \
    -DCMAKE_RANLIB="$LLVM_RANLIB" \
    -DCMAKE_C_FLAGS="$STATIC_CFLAGS" \
    -DCMAKE_CXX_FLAGS="$STATIC_CXXFLAGS" \
    -DCMAKE_EXE_LINKER_FLAGS="-fuse-ld=lld" \
    -DENABLE_SHARED=OFF \
    -DENABLE_CLI=OFF \
    -DSTATIC_LINK_CRT=ON \
    ../source

  ninja -j"$(nproc)"
  ninja install

  # Fix pkgconfig (replace -lgcc_s with -lgcc_eh for static linking)
  sed -i 's/-lgcc_s/-lgcc_eh/g' "$PREFIX/lib/pkgconfig/x265.pc" 2>/dev/null || true

  touch "$x265_dir/build-done"
  REBUILD_FFMPEG=true
fi

# ==========================================
# Build SVT-AV1 (HDR fork)
# ==========================================
echo "=========================================="
echo "Building svt-av1-hdr"
echo "=========================================="
cd "$HOME/repo"
if git_update "https://github.com/juliobbv-p/svt-av1-hdr.git" "svt-av1-hdr" 1; then
  cd "$HOME/repo/svt-av1-hdr"
  rm -rf build && mkdir build && cd build

  cmake -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DCMAKE_C_COMPILER="$CLANG" \
    -DCMAKE_CXX_COMPILER="$CLANGXX" \
    -DCMAKE_AR="$LLVM_AR" \
    -DCMAKE_RANLIB="$LLVM_RANLIB" \
    -DCMAKE_C_FLAGS="$STATIC_CFLAGS" \
    -DCMAKE_CXX_FLAGS="$STATIC_CXXFLAGS" \
    -DCMAKE_EXE_LINKER_FLAGS="-fuse-ld=lld" \
    -DBUILD_SHARED_LIBS=OFF \
    -DBUILD_APPS=ON \
    -DSVT_AV1_LTO=ON \
    ..

  ninja -j"$(nproc)"
  ninja install
  REBUILD_FFMPEG=true
fi

# ==========================================
# Build dav1d
# ==========================================
echo "=========================================="
echo "Building dav1d"
echo "=========================================="
cd "$HOME/repo"
if git_update "https://code.videolan.org/videolan/dav1d.git" "dav1d" 1; then
  cd "$HOME/repo/dav1d"
  rm -rf build

  meson setup build \
    --native-file "$MESON_NATIVE_FILE" \
    --prefix="$PREFIX" \
    --buildtype=release \
    --default-library=static \
    -Denable_tools=false \
    -Denable_tests=false

  ninja -C build -j"$(nproc)"
  ninja -C build install
  REBUILD_FFMPEG=true
fi

# ==========================================
# Build VMAF
# ==========================================
echo "=========================================="
echo "Building VMAF"
echo "=========================================="
cd "$HOME/repo"
if git_update "https://github.com/Netflix/vmaf.git" "vmaf" 1; then
  cd "$HOME/repo/vmaf/libvmaf"
  rm -rf build

  meson setup build \
    --native-file "$MESON_NATIVE_FILE" \
    --prefix="$PREFIX" \
    --buildtype=release \
    --default-library=static \
    -Denable_avx512=true \
    -Denable_float=false

  ninja -C build -j"$(nproc)"
  ninja -C build install
  REBUILD_FFMPEG=true
fi

# ==========================================
# Build freetype
# ==========================================
echo "=========================================="
echo "Building freetype"
echo "=========================================="
cd "$HOME/repo"
if git_update "https://github.com/freetype/freetype.git" "freetype" 1; then
  cd "$HOME/repo/freetype"
  ./autogen.sh

  CFLAGS="$STATIC_CFLAGS" \
  ./configure \
    --prefix="$PREFIX" \
    --disable-shared \
    --enable-static \
    --with-pic \
    --with-zlib=yes \
    --with-png=no \
    --with-bzip2=no \
    --with-harfbuzz=no

  make -j"$(nproc)"
  make install
  REBUILD_FFMPEG=true
fi

# ==========================================
# Build expat (fontconfig needs this)
# ==========================================
echo "=========================================="
echo "Building expat"
echo "=========================================="
cd "$HOME/repo"
if git_update "https://github.com/libexpat/libexpat.git" "libexpat" 1; then
  cd "$HOME/repo/libexpat/expat"
  rm -rf build && mkdir build && cd build

  cmake -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DCMAKE_C_COMPILER="$CLANG" \
    -DCMAKE_C_FLAGS="$STATIC_CFLAGS" \
    -DBUILD_SHARED_LIBS=OFF \
    -DEXPAT_BUILD_DOCS=OFF \
    -DEXPAT_BUILD_EXAMPLES=OFF \
    -DEXPAT_BUILD_TESTS=OFF \
    ..

  ninja -j"$(nproc)"
  ninja install
  REBUILD_FFMPEG=true
fi

# ==========================================
# Build fontconfig
# ==========================================
echo "=========================================="
echo "Building fontconfig"
echo "=========================================="
cd "$HOME/repo"
if git_update "https://gitlab.freedesktop.org/fontconfig/fontconfig.git" "fontconfig" 1; then
  cd "$HOME/repo/fontconfig"
  rm -rf build

  meson setup build \
    --native-file "$MESON_NATIVE_FILE" \
    --prefix="$PREFIX" \
    --buildtype=release \
    --default-library=static \
    -Ddoc=disabled \
    -Dtests=disabled

  ninja -C build -j"$(nproc)"
  ninja -C build install
  REBUILD_FFMPEG=true
fi

# ==========================================
# Build fribidi
# ==========================================
echo "=========================================="
echo "Building fribidi"
echo "=========================================="
cd "$HOME/repo"
if git_update "https://github.com/fribidi/fribidi.git" "fribidi" 1; then
  cd "$HOME/repo/fribidi"
  rm -rf build

  meson setup build \
    --native-file "$MESON_NATIVE_FILE" \
    --prefix="$PREFIX" \
    --buildtype=release \
    --default-library=static \
    -Ddocs=false

  ninja -C build -j"$(nproc)"
  ninja -C build install
  REBUILD_FFMPEG=true
fi

# ==========================================
# Build harfbuzz
# ==========================================
echo "=========================================="
echo "Building harfbuzz"
echo "=========================================="
cd "$HOME/repo"
if git_update "https://github.com/harfbuzz/harfbuzz.git" "harfbuzz" 1; then
  cd "$HOME/repo/harfbuzz"
  rm -rf build

  meson setup build \
    --native-file "$MESON_NATIVE_FILE" \
    --prefix="$PREFIX" \
    --buildtype=release \
    --default-library=static \
    -Ddocs=disabled \
    -Dtests=disabled \
    -Dfreetype=enabled

  ninja -C build -j"$(nproc)"
  ninja -C build install
  REBUILD_FFMPEG=true
fi

# ==========================================
# Build libass
# ==========================================
echo "=========================================="
echo "Building libass"
echo "=========================================="
cd "$HOME/repo"
if git_update "https://github.com/libass/libass.git" "libass" 1; then
  cd "$HOME/repo/libass"
  # Run libtoolize explicitly first, then copy ltmain.sh if it was put in wrong place
  libtoolize --copy --force
  [[ -f ltmain.sh ]] || cp -f "$(dirname "$(which libtoolize)")/../share/libtool/build-aux/ltmain.sh" . 2>/dev/null || true
  ./autogen.sh

  CFLAGS="$STATIC_CFLAGS -I$PREFIX/include/harfbuzz -I$PREFIX/include/fribidi -I$PREFIX/include/freetype2" \
  LDFLAGS="-L$PREFIX/lib" \
  PKG_CONFIG_PATH="$PKG_CONFIG_PATH" \
  ./configure \
    --prefix="$PREFIX" \
    --disable-shared \
    --enable-static \
    --with-pic

  make -j"$(nproc)"
  make install
  REBUILD_FFMPEG=true
fi

# ==========================================
# Build FFmpeg
# ==========================================
echo "=========================================="
echo "Building FFmpeg (static, LTO)"
echo "=========================================="
cd "$HOME/repo"

if download_if_changed "https://ffmpeg.org/releases/ffmpeg-snapshot.tar.bz2" "ffmpeg-snapshot.tar.bz2"; then
  rm -rf "$HOME/repo/ffmpeg"
  tar -xf "$CACHE_DIR/ffmpeg-snapshot.tar.bz2" -C "$HOME/repo"
  REBUILD_FFMPEG=true
fi

if [[ "$FORCE_FFMPEG" == "1" ]]; then
  echo "FORCE_FFMPEG=1 set: rebuilding FFmpeg"
  REBUILD_FFMPEG=true
fi

if $REBUILD_FFMPEG || [[ ! -f "$PREFIX/bin/ffmpeg" ]]; then
  cd "$HOME/repo/ffmpeg"

  if [[ "$FORCE_FFMPEG_CLEAN" == "1" ]]; then
    echo "FORCE_FFMPEG_CLEAN=1: cleaning FFmpeg"
    make distclean 2>/dev/null || true
  fi

  # Configure for fully static build
  PKG_CONFIG_PATH="$PKG_CONFIG_PATH" \
  ./configure \
    --prefix="$PREFIX" \
    --bindir="$PREFIX/bin" \
    --cc="$CLANG" \
    --cxx="$CLANGXX" \
    --ld="$CLANG" \
    --ar="$LLVM_AR" \
    --ranlib="$LLVM_RANLIB" \
    --nm="$LLVM_NM" \
    --strip="$LLVM_STRIP" \
    --pkg-config="$PKG_CONFIG_BIN" \
    --pkg-config-flags="--static" \
    --extra-cflags="-I$PREFIX/include -I$PREFIX/include/freetype2" \
    --extra-ldflags="-L$PREFIX/lib -L$PREFIX/lib64 -L$GCC_LIB_PATH -fuse-ld=lld -Wl,-rpath,$GCC_LIB_PATH" \
    --extra-libs="-lpthread -lm -ldl -lstdc++" \
    --enable-static \
    --disable-shared \
    --enable-pic \
    --enable-lto \
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
    --enable-libdav1d \
    --enable-libvmaf \
    --enable-libass \
    --enable-libfribidi \
    --enable-libharfbuzz \
    --disable-debug \
    --enable-runtime-cpudetect \
    --disable-doc

  make -j"$(nproc)"
  make install
fi

echo ""
echo "=========================================="
echo "Build complete!"
echo "=========================================="
echo ""
"$PREFIX/bin/ffmpeg" -version
echo ""
echo "Binary type:"
file "$PREFIX/bin/ffmpeg"
echo ""
echo "Shared library dependencies:"
ldd "$PREFIX/bin/ffmpeg" 2>&1 || echo "(statically linked)"
