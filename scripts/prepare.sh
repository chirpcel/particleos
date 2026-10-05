#!/bin/bash
# SPDX-License-Identifier: LGPL-2.1-or-later
set -euo pipefail
stage=$(mktemp -d .aur-prepare-XXXXXX)
trap 'rm -rf -- "$stage"' EXIT
mkdir -p "$stage/packages" mkosi.conf.d
filename=sdme-0.21.0-1-x86_64.pkg.tar.zst
package=$stage/packages/$filename
checksum=d8b844cabf8a659b2061745e2ae1c6509308ab1a2f9405fc1c8e850ffddd0455
if printf '%s  %s\n' "$checksum" "mkosi.packages/$filename" | sha256sum --check --status 2>/dev/null; then
    cp -- "mkosi.packages/$filename" "$package"
else
    curl --fail --location --proto '=https' --proto-redir '=https' \
        --retry 3 --connect-timeout 30 --max-time 300 \
        --output "$package" \
        https://github.com/fiorix/sdme/releases/download/v0.21.0/sdme-0.21.0-1-x86_64.pkg.tar.zst
    printf '%s  %s\n' "$checksum" "$package" | sha256sum --check --status
fi
printf '[Config]\nProfiles=aur-builder\n\n[Content]\n' >"$stage/90-aur.conf"
read -ra packages <<<"${AUR_PACKAGES//$'\n'/ }"
for name in "${packages[@]}"; do
    [[ $name =~ ^[a-z0-9][a-z0-9@._+-]{0,127}$ && $name != sdme ]] ||
        { echo "Invalid AUR package base: $name" >&2; exit 1; }
    particleos-aur --output "$stage/packages" "$name"
    printf 'Packages=%s\n' "$name" >>"$stage/90-aur.conf"
done
# Track package identities, not filename prefixes: foo must not prune foo-bar.
printf '%s  %s\n' "$checksum" "$package" | sha256sum --check --status
for archive in "$stage/packages/"*.pkg.tar.*; do
    identity=$(pacman -Qp -- "$archive")
    printf '%s\n' "${identity%% *}"
done | sort -u >"$stage/names"
cat "$stage/names" >"$stage/managed"
if [[ -f mkosi.packages/.particleos-aur-names ]]; then
    cat mkosi.packages/.particleos-aur-names >>"$stage/managed"
fi
mkdir -p mkosi.packages
: >"$stage/stale"
for archive in mkosi.packages/*.pkg.tar.*; do
    [[ -f $archive ]] || continue
    identity=$(pacman -Qp -- "$archive" 2>/dev/null) || continue
    if grep -Fxq "${identity%% *}" "$stage/managed"; then
        [[ -f $stage/packages/${archive##*/} ]] || printf '%s\n' "$archive" >>"$stage/stale"
    fi
done
# Publish after all builds; prune only managed identities, preserving unrelated
# local packages. No host package installation is performed by pacman -Qp.
for archive in "$stage/packages/"*.pkg.tar.*; do
    mv -T -- "$archive" "mkosi.packages/${archive##*/}"
done
while IFS= read -r archive; do rm -f -- "$archive"; done <"$stage/stale"
mv -T -- "$stage/names" mkosi.packages/.particleos-aur-names
mv -T -- "$stage/90-aur.conf" mkosi.conf.d/90-aur.conf
