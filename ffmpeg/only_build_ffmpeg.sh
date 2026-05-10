#!/bin/bash
set -euo pipefail

PREFIX="${PREFIX:-$HOME/ffmpeg_build}"

mkdir -p "$PREFIX"/{bin,lib,lib64,include,share,lib/pkgconfig,lib64/pkgconfig}

if [[ -z "${IN_NIX_SHELL:-}" ]]; then
	echo "ERROR: enter the project dev shell first with: nix develop" >&2
	exit 1
fi

require_tool() {
	local name="$1"
	local path
	path="$(command -v "$name" 2>/dev/null || true)"
	if [[ -z "$path" ]]; then
		echo "ERROR: missing required tool: $name" >&2
		exit 1
	fi
	echo "$path"
}

CLANG="$(require_tool clang)"
CLANGXX="$(require_tool clang++)"
LLVM_AR="$(require_tool llvm-ar)"
LLVM_RANLIB="$(require_tool llvm-ranlib)"
LLVM_NM="$(require_tool llvm-nm)"
LLVM_STRIP="$(require_tool llvm-strip)"
PKG_CONFIG_BIN="$(require_tool pkg-config)"

default_pkg_config_path="$PREFIX/lib/pkgconfig:$PREFIX/lib64/pkgconfig:$PREFIX/lib/x86_64-linux-gnu/pkgconfig"
export PKG_CONFIG_PATH="${PKG_CONFIG_PATH:-$default_pkg_config_path}"

_clang_verbose="$("$CLANGXX" -v /dev/null -o /dev/null 2>&1 || true)"
GCC_LIB_PATH=$(printf '%s\n' "$_clang_verbose" | grep -oP '(?<=-L)/nix/store/[^/]+-gcc-[^/]+-lib/lib' | head -1 || true)

extra_ldflags="-L$PREFIX/lib -L$PREFIX/lib64"
if [[ -n "$GCC_LIB_PATH" ]]; then
	extra_ldflags="$extra_ldflags -L$GCC_LIB_PATH -fuse-ld=lld -Wl,-rpath,$GCC_LIB_PATH"
else
	extra_ldflags="$extra_ldflags -fuse-ld=lld"
fi

cd "$HOME/repo/ffmpeg"

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
	--extra-ldflags="$extra_ldflags" \
	--extra-libs="-lpthread -lm -ldl -lstdc++" \
	--enable-static \
	--disable-shared \
	--enable-pic \
	--enable-lto \
	--enable-gpl \
	--enable-nonfree \
	--disable-bzlib \
	--disable-lzma \
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
	--enable-libzimg \
	--enable-runtime-cpudetect \
	--disable-debug \
	--disable-doc

make -j"$(nproc)"
make install
