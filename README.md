# ⸭ ParticleOS

ParticleOS is a fully customizable immutable distribution implementing the
concepts described in
[Fitting Everything Together](https://0pointer.net/blog/fitting-everything-together.html).

Note that ParticleOS is still in development, and we don't provide any backwards
compatibility guarantees at all.

The crucial difference that makes ParticleOS unique compared to other immutable
distributions is that users build the ParticleOS image themselves and sign it
with their own keys instead of installing vendor signed images. This allows
configuring the image to your liking by having full control over which
packages are installed into the Arch Linux-based image.

The ParticleOS image is built using [mkosi](https://github.com/systemd/mkosi).
You will need to install the current main branch of mkosi to build current
ParticleOS images.

First, configure the variant you'd like to build in `mkosi.local.conf`. For a
desktop system, you'll want the `desktop` and one of `gnome`, `kde`, or
`sway` profiles.

```conf
[Distribution]
Distribution=arch

[Config]
Profiles=desktop,kde
```

It is also strongly recommended to write a hashed root password prefixed with
`hashed:` to `mkosi.rootpw` to allow debugging the system if something breaks.

To build the image, run `mkosi -B -f` from the ParticleOS repository.
Only Arch Linux (`Distribution=arch`) is supported.

To update the system after installation, you clone the ParticleOS repository
or your fork of it, make sure `mkosi.local.conf` is configured to your liking and
run `mkosi -B -ff sysupdate -- update --reboot` which will update the system using
`systemd-sysupdate` and then reboot.

## Building AUR packages

Configure the space-separated `AUR_PACKAGES` list in `mise.toml` with AUR package
bases that produce a same-named package, then run:

```sh
mise run prepare
mkosi -B -f
```

`prepare` assumes `sdme` and `particleos-aur` are already installed on the build
host; it does not bootstrap them. It downloads the checksum-pinned native Arch
sdme package and builds the configured AUR packages into `mkosi.packages/`.
It also selects those packages for installation in the image. mkosi uses its
normal local-package repository and Pacman installation; there is no custom
binary installer. The `aur-builder` profile includes sdme and the helper in
subsequent images and is enabled by default; retain it if you override `Profiles=`
in `mkosi.local.conf`. Preparation also requires curl, sha256sum, and Pacman for
read-only package metadata queries. It never installs packages on the build host.

For a single build, use `particleos-aur --output /path/to/packages PKGBASE`.
Use `particleos-aur --reset` to remove your container and its imported base.
The helper always uses a fresh checkout and asks for review before building.

`particleos-aur` uses [sdme](https://github.com/fiorix/sdme) to create or reuse an
independent mutable Arch container, runs `makepkg` as an unprivileged guest user,
and exports packages without installing anything on the host. Administrative
authorization is needed for sdme operations. Dependencies available only in the
AUR are not resolved automatically.

AUR recipes execute arbitrary code: review and trust the configured packages.
Containers share the host kernel, so use a VM for untrusted recipes. `/usr`
remains immutable; container state lives under `/var/lib/sdme/` and can be lost
during factory reset. Internet access and working host networkd are required.
IPE enforcement may prevent execution of mutable guest binaries; the builder
never disables it.

## Using the OBS profile to fetch a newer systemd

Sometimes ParticleOS adopts systemd features as soon as they get merged into
systemd without waiting for an official release. That's why we recommend
enabling the `obs-repos` profile to enable the systemd repositories on OBS
(https://software.opensuse.org//download.html?project=system%3Asystemd&package=systemd)
containing systemd packages which are built every day from systemd's git main
branch.

To enable the `obs-repos` profile, add the following to `mkosi.local.conf`:

```conf
[Config]
Profiles=obs-repos
```

We also provide the `obs-repos-stable` profile, that will use the latest stable
branch of systemd, instead of main, providing more stability and less risk, as
it is what distributions typically use. To enable this profile, add the
following to `mkosi.local.conf`:

```conf
[Config]
Profiles=obs-repos-stable
```

## Building systemd from source

As an alternative to using the `obs-repos` profile, you can build systemd from source:

```sh
git clone https://github.com/systemd/systemd
cd systemd
mkosi -f sandbox -- meson setup build
mkosi -f sandbox -- meson compile -C build
mkosi -t none -f
```

Then write the following to `mkosi.local.conf` in the ParticleOS repository to
use the artifacts from the systemd repository built by mkosi in ParticleOS:

```conf
[Content]
VolatilePackageDirectories=../systemd/build/mkosi.builddir/<distribution>~<release>~<arch>

[Build]
ExtraSearchPaths=../systemd/build
```

Make sure the distribution and release in `mkosi.local.conf` are identical in the
systemd checkout and the particleos checkout.

To build a newer systemd, run `git pull` in the systemd repository followed by
 `mkosi -f sandbox -- meson compile -C build` and `mkosi -t none`.

## Signing keys

ParticleOS images are signed for Secure Boot with the user's keys. To generate a new key,
run `mkosi genkey`. The key must be stored safely, it will be required to sign updates.

The key can be stored in a smartcard. Then you have to set the key in `mkosi.local.conf`:

```
[Validation]
SecureBootKey=pkcs11:object=Private key 1;type=private
SecureBootKeySource=provider:pkcs11
SignExpectedPcrKey=pkcs11:object=Private key 1;type=private
SignExpectedPcrKeySource=provider:pkcs11
VerityKey=pkcs11:object=Private key 1;type=private
VerityKeySource=provider:pkcs11
```

With a YubiKey you can generate a key and certificate in PIV:

```sh
ykman piv keys generate --algorithm RSA2048 9c pubkey.pem
ykman piv certificates generate --subject "CN=mkosi" 9c pubkey.pem
rm pubkey.pem
pkcs11-tool --module /usr/lib/x86_64-linux-gnu/opensc-pkcs11.so --list-objects --type cert
# Should print something like:
Using slot 0 with a present token (0x0)
Certificate Object; type = X.509 cert
  label:      Certificate for Digital Signature
  subject:    DN: CN=mkosi
  serial:     ...
  ID:         02
  uri:        pkcs11:model=PKCS%2315%20emulated;manufacturer=piv_II;serial=...;token=mkosi;id=%02;object=Certificate%20for%20Digital%20Signature;type=cert
```

Then you have to set the key with the right token and key ID in `mkosi.local.conf`:

```
[Validation]
SecureBootKey=pkcs11:token=mkosi;id=%%02;type=private
SecureBootKeySource=provider:pkcs11
SecureBootCertificate=pkcs11:token=mkosi;id=%%02;type=cert
SecureBootCertificateSource=provider:pkcs11
SignExpectedPcrKey=pkcs11:token=mkosi;id=%%02;type=private
SignExpectedPcrKeySource=provider:pkcs11
SignExpectedPcrCertificate=pkcs11:token=mkosi;id=%%02;type=cert
SignExpectedPcrCertificateSource=provider:pkcs11
VerityKey=pkcs11:token=mkosi;id=%%02;type=private
VerityKeySource=provider:pkcs11
VerityCertificate=pkcs11:token=mkosi;id=%%02;type=cert
VerityCertificateSource=provider:pkcs11
```

## Installation

Before installing ParticleOS, make sure that Secure Boot is in *setup*
*mode* on the target system. The Secure Boot mode can be configured in
the UEFI firmware interface of the target system. If there's an
existing Linux installation on the target system already, run
`systemctl reboot --firmware-setup` to reboot into the UEFI firmware
interface. At the same time, make sure the UEFI firmware interface is
password protected so an attacker cannot just disable Secure Boot
again.

To install ParticleOS with a USB drive, first build the image on an
existing Linux system as described above. Then, write it to the USB
drive with `mkosi burn /dev/<usb>`. Once written to the USB drive, plug
the USB drive into the system onto which you'd like to install
ParticleOS and boot into the USB drive via the firmware menu. Then,
boot into the "Installer" UKI profile, which runs
`systemd-sysinstall`. It will prompt for the target drive and any
other details required, then partition the disk, copy ParticleOS onto
it, set up the ESP via `bootctl install` and finally install a kernel
via `bootctl link`. Once it completes, reboot into the target drive
(i.e not the USB drive) and the default profile (i.e. not the
installer one) to complete the installation.

If you prefer to drive the install manually, boot into the "Live
System" UKI profile instead. When you end up in the root shell, run
`systemd-sysinstall` to install ParticleOS to the system's drive,
then reboot as above. If you invoke `systemd-sysinstall` without
arguments it will interactively query you for configuration
parameters, as necessary. You may alternatively configure the new
installation with command line parameters of the tool, see the
systemd-sysinstall(8) man page for details.

## LUKS recovery key

systemd doesn't support adding a recovery key to a partition enrolled with a token
only (tpm/fido2). It is possible to use cryptenroll to add a recovery password
to the root partition: `cryptsetup luksAddKey --token-type systemd-tpm2 /dev/<id>`

## Firmwares

Only firmwares that are dependencies of a kernel module are included, but some
modules don't declare their dependencies properly. Dependencies of a module can be
found with `modinfo`. If you experience missing firmwares, you should report
this to the module maintainer. `FirmwareInclude=` can be added in `mkosi.local.conf`
to include the firmware regardless of whether a module depends on it.

## Configuring systemd-homed after installation

After installing ParticleOS and logging into your systemd-homed managed user,
run the following to configure systemd-homed for the best experience:

```sh
homectl update \
    --auto-resize-mode=off \
    --disk-size=max \
    --luks-discard=on"
```

Disabling the auto resize mode avoids slow system boot and shutdown. Enabling
LUKS discard makes sure the home directory doesn't become inaccessible because
systemd-homed is unable to resize the home directory.

## Default root password and user when booting in a virtual machine

If you boot ParticleOS in a virtual machine using `mkosi vm`, the root password
is automatically set to `particleos` and a default user `particleos` with password
`particleos` is created as well.
