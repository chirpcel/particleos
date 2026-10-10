# ⸭ ParticleOS

ParticleOS is a customizable, immutable Arch Linux system built with
[mkosi](https://github.com/systemd/mkosi). You choose the packages, build the
image, and sign it with your own keys. This repository configures an Intel
laptop with a Sway desktop and development tools by default.

The design follows systemd's
[Fitting Everything Together](https://0pointer.net/blog/fitting-everything-together.html):
a signed operating system image, separate writable state, and image-based
updates.

**ParticleOS is under development and provides no backwards compatibility
guarantees.** Only Arch Linux images are supported.

## How it works

- **Immutable operating system:** `/usr` is a compressed EROFS partition with
  dm-verity data and a signature. Package changes happen when building an image.
- **User-controlled signing:** Secure Boot, expected PCR signatures, and verity
  signatures use your keys. The checked-in configuration selects a PKCS#11 token.
- **Writable state:** installation repartitioning defines a TPM2-encrypted
  Btrfs root, encrypted swap, and a separate Btrfs home partition.
  User homes are managed with systemd-homed.
- **Image updates:** systemd-sysupdate transfers the new `/usr`, verity artifacts,
  and unified kernel image (UKI). The layout provides two sets of `/usr`
  partitions, and UKI updates use boot counting.
- **Applications and development:** the desktop includes Flatpak and a Flathub
  remote definition; the development profile includes Podman, mise, and an
  sdme-based AUR package builder.

The base uses `linux-hardened` and includes AppArmor. IPE enforcement is disabled
in the default boot command line; a separate UKI profile requests enforcement.
These are configured features, not a guarantee that every hardware and software
combination has been validated.

## Build an image

### Prerequisites

Use an existing Linux build host with a current main-branch version of mkosi.
[`mkosi.conf`](mkosi.conf) requires at least `26~devel`; new systemd features
such as `systemd-sysinstall` must also be available in the image's packages.
The build downloads Arch packages and a mkosi tools tree.

For the optional AUR preparation workflow, use **Arch Linux x86_64** with
mise, sdme, and `particleos-aur` already installed, plus curl, sha256sum, and
Pacman. Preparation does not bootstrap the host tools.

```sh
git clone https://github.com/chirpcel/particleos.git
cd particleos
```

### Choose profiles and packages

The default profiles are `intel,laptop,desktop,devel`. The profiles available
in this checkout are:

| Profile | Contents |
| --- | --- |
| `intel` | Intel microcode, firmware, graphics, media, and OpenVINO packages |
| `laptop` | Wi-Fi, Bluetooth, brightness, WWAN, and power management packages |
| `desktop` | Sway with UWSM, greetd/tuigreet, Foot, Fuzzel, PipeWire, and Flatpak |
| `devel` | Git, mise, Podman, krun, sdme, and the `particleos-aur` helper |

Put local overrides in `mkosi.local.conf`. For example, to build the desktop
and development environment without the Intel and laptop package selections:

```ini
[Config]
Profiles=desktop,devel

[Content]
Packages=htop
```

Retain `devel` if you want its tools in the resulting image. There are no
separate `gnome`, `kde`, `sway`, `obs-repos`, or `obs-repos-stable` profiles
in this checkout; Sway is part of `desktop`.

### Configure signing

The defaults select the private key and certificate with PKCS#11 URI
`pkcs11:token=Database Key;id=%%02`, using `provider:pkcs11` for all three
signing purposes. Use that token, or override the key, certificate, and source
settings in `mkosi.local.conf` to match your own hardware token. Keep the
double percent signs when specifying the key ID in mkosi configuration.

To use local file-based keys instead, first generate them:

```sh
mkosi genkey
```

Then add these overrides to `mkosi.local.conf`:

```ini
[Validation]
SecureBootKey=mkosi.key
SecureBootKeySource=file
SecureBootCertificate=mkosi.crt
SecureBootCertificateSource=file
SignExpectedPcrKey=mkosi.key
SignExpectedPcrKeySource=file
SignExpectedPcrCertificate=mkosi.crt
SignExpectedPcrCertificateSource=file
VerityKey=mkosi.key
VerityKeySource=file
VerityCertificate=mkosi.crt
VerityCertificateSource=file
```

Keep your private key safe and backed up: you need it to sign future updates.
Generating files alone does not replace the repository's PKCS#11 defaults.

For emergency debugging, it is also strongly recommended to put a hashed root
password, prefixed with `hashed:`, in `mkosi.rootpw`.

### Build and try it

```sh
mkosi -B -f
mkosi vm
```

Artifacts go to `mkosi.output/`, with names based on
`ParticleOS_<version>_<architecture>`. The build produces a disk image, a
split UKI, partition artifacts, and a JSON manifest. Build caches live in
`mkosi.cache/`; `mkosi.bump` generates UTC timestamp versions.

The VM defaults are 4 CPUs, 4 GiB RAM, a 30 GiB runtime disk, and ephemeral
execution. In `mkosi vm`, the root password is `particleos`; the supplied
home credential creates a `particleos` user with password `particleos`.
Runtime credentials also enable console autologin. These are development
defaults.

## Optional: include AUR packages

Set the whitespace-separated `AUR_PACKAGES` value in [`mise.toml`](mise.toml)
to the AUR package bases you want. Each base must emit a same-named package.

```sh
mise run prepare
mkosi -B -f
```

[`scripts/prepare.sh`](scripts/prepare.sh) downloads the checksum-pinned
sdme 0.21.0 x86_64 Arch package and invokes `particleos-aur` for each configured
base. Even an empty AUR list stages sdme. It publishes archives into
`mkosi.packages/` and writes `mkosi.conf.d/90-aur.conf` to select the
development profile and requested packages. mkosi installs these through its
normal local-package repository and Pacman workflow.

Preparation publishes packages after all builds succeed and prunes stale
archives for package identities it manages, preserving unrelated local packages.
It does not install packages on the build host.

For a standalone build:

```sh
particleos-aur --output /path/to/packages PKGBASE
```

The helper runs as an ordinary user and uses `run0` to authorize sdme
operations. It imports an Arch base and creates or reuses a mutable container
with user namespaces, then runs `makepkg` as an unprivileged guest user.
Each invocation checks out fresh AUR sources and prompts for review before
building. Dependencies available only in the AUR are not resolved automatically.

AUR recipes execute arbitrary code. Review the recipes; containers share the
host kernel, so use a VM for untrusted builds. Internet access and working host
systemd-networkd are required. IPE enforcement may block mutable guest binaries;
the helper never disables it.

To remove your builder container and its imported base:

```sh
particleos-aur --reset
```

Container state lives under `/var/lib/sdme/` and can be lost during factory reset.

## Install on hardware

Back up the target machine before installation. Writing the USB image and
installing to the target disk overwrite data.

1. Put Secure Boot into **setup mode** in the target's UEFI firmware. From an
   existing Linux installation, `systemctl reboot --firmware-setup` can open
   the firmware interface. Protect firmware settings with a password.
2. Build the image and write it to the intended USB drive:
   `mkosi burn /dev/<usb>`.
3. Boot the USB drive and select the **Installer** UKI profile.
   `systemd-sysinstall` prompts for the target drive and configuration,
   partitions the disk, copies the system, and installs the boot loader and UKI.
4. Reboot from the target disk into the default profile.

For a manual installation, select **Live System** and run
`systemd-sysinstall` from its root shell. Run it without arguments for
interactive configuration, or consult `systemd-sysinstall(8)` for options.
The live profile provides root autologin and the root password `particleos`.

## Update an installed system

Clone this repository or your fork on the installed system, restore your local
configuration, and use the same signing identity as your installed image.
If you use AUR packages, run `mise run prepare` before building the update.

```sh
mkosi -B -ff sysupdate -- update --reboot
```

This rebuilds the image, applies it with systemd-sysupdate, and reboots.
The transfer definitions in [`mkosi.sysupdate/`](mkosi.sysupdate/) consume
locally built artifacts. To change system packages, edit the image configuration
and rebuild; `/usr` and the Pacman local database reside in the immutable image.

## Recovery and troubleshooting

The [UKI profiles](mkosi.uki-profiles/) include Live System, Installer, IPE
enforcement, emergency mode, debug logging, storage target mode, and factory
reset options. **Factory reset can erase writable root and home state**;
one option also clears the TPM2. Storage target mode explicitly provides public
access. Choose these profiles deliberately.

For a TPM2-enrolled LUKS root, the existing token can authorize adding a recovery
passphrase:

```sh
cryptsetup luksAddKey --token-type systemd-tpm2 /dev/<root-partition>
```

For systemd-homed tuning, after logging in as the managed user, update that
user's home:

```sh
homectl update "$USER" \
    --auto-resize-mode=off \
    --disk-size=max \
    --luks-discard=on
```

Disabling automatic resizing avoids resizing delays at boot and shutdown.
Allowing LUKS discard supports space reclamation in the home image.

If firmware is missing, check the module's dependencies with `modinfo`.
Only firmware associated with included kernel modules is normally included;
add `FirmwareInclude=` under `[Content]` in `mkosi.local.conf` when a module
does not declare its firmware correctly.

## Repository layout

| Path | Purpose |
| --- | --- |
| [`mkosi.conf`](mkosi.conf) | Base packages, default profiles, signing, output, and VM settings |
| [`mkosi.profiles/`](mkosi.profiles/) | Optional package selections and profile-specific files |
| [`mkosi.extra/`](mkosi.extra/) | Boot, systemd, networking, and factory configuration |
| [`mkosi.repart/`](mkosi.repart/) | Build-time disk partitions and verity artifacts |
| [`mkosi.sysupdate/`](mkosi.sysupdate/) | Installed-system update transfers |
| [`mkosi.uki-profiles/`](mkosi.uki-profiles/) | Alternate boot and recovery modes |
| `mkosi.postinst*`, `mkosi.finalize` | Arch validation, branding, Pacman database relocation, and factory defaults |
| [`mise.toml`](mise.toml), [`scripts/prepare.sh`](scripts/prepare.sh) | Local package preparation |

Local configuration, keys, outputs, caches, and prepared packages are excluded
from Git by [`.gitignore`](.gitignore).

## License

GNU LGPL 2.1 or later; see [LICENSE](LICENSE) and the source file SPDX headers.
