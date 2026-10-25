# Rebuilding the gateway box from scratch

The STB (Amlogic S905, B860H class) has been wiped once (2026-10-25) and came
back on a stock Ubuntu 18.04 / Python 3.6 image. This is what it took; the
service itself is only `app/` + `requirements.txt` + the unit in this folder.

1. **SSH**: put a key in `/root/.ssh/authorized_keys` (check the fingerprint with
   `ssh-keygen -lf`; a key from another session is not yours).
2. **apt**: a stale `apt.armbian.com` source breaks `apt-get update` -- comment it out.
   `apt-get install -y tailscale ufw` (Tailscale's own bionic repo).
3. **Python**: the OS Python is too old (needs 3.9+ for `zoneinfo`). Use `uv`'s
   *musl* build (the default build needs glibc 2.28, bionic has 2.27):
   `uv-aarch64-unknown-linux-musl` into `/opt/uv/bin`, then
   `UV_PYTHON_INSTALL_DIR=/opt/uv/python uv python install 3.12`.
4. **Code + venv**: `useradd --system --home-dir /opt/scr100-gateway scr100`, rsync this
   repo (no `.git`/`.venv`) to `/opt/scr100-gateway`, `uv venv --python <3.12> .venv`,
   `uv pip install --python .venv/bin/python -r requirements.txt`, chown to `scr100`.
5. **Config**: `/etc/scr100-gateway.env` from `env.example`; unit from
   `scr100-gateway.service` into `/etc/systemd/system/`; `systemctl enable --now`.
6. **Firewall**: `ufw allow 22/tcp`, `ufw allow in on tailscale0 to any port 8768 proto tcp`,
   then enable. Allow SSH *first*.
7. **Tailscale**: `tailscale up --hostname=scr100-gateway --accept-dns=false
   --accept-routes=false`, approve with the owning account. Then in the admin
   console: delete the dead old node, set this node's IPv4 to the one
   card-pipeline is configured with (`100.118.141.16`), and **disable key expiry**.
   Never `pkill -f "tailscale up"` over SSH -- the pattern matches your own shell.
8. Timezone `Asia/Jakarta`.

## Traps this box has

- **Soft `reboot` hangs; it needs the power cut.** A cold power-up works fine
  (verified 2026-10-25), so "unplug, plug in" is the recovery.
- **No battery RTC.** It boots with `fake-hwclock`'s last saved time (seen 17 min
  stale; after a move, days) and chrony fixes it ~10s later. The unit therefore
  runs `chronyc waitsync` before starting, because the startup hook pushes this
  host's clock onto the SCR100. Do not remove it.
- The SCR100 has no timezone concept; `_local_now()` pushes Jakarta wall-clock
  time regardless of the host's zone.

## Hardening applied 2026-10-25 (re-apply after a rebuild)

- **SSH**: `PasswordAuthentication no`, `PermitRootLogin prohibit-password`,
  `X11Forwarding no`, `MaxAuthTries 3`. Put an authorized key in *before* turning
  password login off, and verify a fresh key login before closing the old session
  (the first rebuild used a 150s auto-revert as a safety net). The box then
  accepts only the keys in `/root/.ssh/authorized_keys`.
- **journald**: `Storage=persistent`, `SystemMaxUse=50M`. The stock config keeps the
  journal in RAM, so a power cut destroyed the only evidence of why it died.
- **Not done on purpose**: the root fs mounts `data=writeback` (set by `rootflags=` in
  `/boot/extlinux/extlinux.conf`; ext4 cannot change data mode on remount). It keeps the
  filesystem consistent after a power cut but a just-written file can come back empty.
  Left alone because a typo in the bootloader config of a remote box means a box that
  will not boot, and it is unclear whether `extlinux.conf` or `emmc_autoscript.cmd` is
  the one actually read. Change it only with physical/serial access.
- **Known gaps**: the OS is EOL Ubuntu 18.04 (~157 security updates pending, not applied --
  a blanket upgrade could break this board's boot); `scr100` still has a login shell
  (`/bin/bash`); no fail2ban (moot with key-only SSH).
