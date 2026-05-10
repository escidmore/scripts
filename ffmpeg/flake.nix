{
  description = "FFmpeg build dev shell";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { nixpkgs, ... }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forEachSystem =
        f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      devShells = forEachSystem (pkgs: {
        default = pkgs.mkShell {
          packages = with pkgs; [
            pkg-config
            cmake
            ninja
            meson
            nasm
            yasm
            autoconf
            automake
            libtool
            git
            curl
            wget
            clang
            llvm
            lld
            zlib
            gnutls
            nettle
            openssl
            glib
            libsysprof-capture
            pcre2
            expat
            graphite2
            libidn2
            p11-kit
            libtasn1

            libopus
            lame
            libvorbis
            libogg
            fdk_aac

            libass
            freetype
            fontconfig
            harfbuzz
            fribidi
            libpng

            libvpx
            numactl
            SDL2
          ];

          shellHook = ''
            export PREFIX="$HOME/ffmpeg_build"
            mkdir -p "$PREFIX"/{bin,lib,lib64,include,share,lib/pkgconfig,lib64/pkgconfig}

            export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig:$PREFIX/lib64/pkgconfig:$PREFIX/lib/x86_64-linux-gnu/pkgconfig''${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
            export CPATH="$PREFIX/include''${CPATH:+:$CPATH}"
            export LIBRARY_PATH="$PREFIX/lib:$PREFIX/lib64''${LIBRARY_PATH:+:$LIBRARY_PATH}"

            export BASE_CFLAGS="-O3 -march=znver4 -mtune=znver4 -pipe -fno-plt"
            export BASE_CXXFLAGS="-O3 -march=znver4 -mtune=znver4 -pipe"
            export CFLAGS="$BASE_CFLAGS -I$PREFIX/include"
            export CXXFLAGS="$BASE_CXXFLAGS -I$PREFIX/include"
            export LDFLAGS="-fuse-ld=lld -L$PREFIX/lib -L$PREFIX/lib64"

            export CC=clang
            export CXX=clang++
            export LD=clang
            export AR=llvm-ar
            export RANLIB=llvm-ranlib
            export NM=llvm-nm
            export STRIP=llvm-strip

            export ACLOCAL_PATH="${pkgs.automake}/share/aclocal''${ACLOCAL_PATH:+:$ACLOCAL_PATH}"

            echo "FFmpeg dev shell active"
          '';
        };
      });
    };
}
