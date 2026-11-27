# Optional: keep the archive on an encrypted drive

This puts the archive on its own LUKS-encrypted partition, so someone who takes the drive can't read it. The key lives
on the server's internal disk, so the drive unlocks by itself at boot. A backup passphrase and a copy of the key on
another computer cover a lost or dead server. Other partitions on the drive aren't touched.

[← Back to installing](install.md)

**This erases the partition you pick.** Use a partition that is empty, or whose data you have already moved off.
Find it:
```
lsblk -f                      # note the partition, e.g. /dev/sda3, and its LABEL
ls -la /media/$USER/<LABEL>   # confirm nothing you need is on it
sudo blkid /dev/sda3          # double-check it's the right one
```
In the commands below, replace `/dev/sda3` with your partition.

## Encrypt and format

`luksFormat` asks you to type `YES`.
```
sudo apt install -y cryptsetup systemd-cryptsetup
sudo umount /dev/sda3
sudo dd if=/dev/urandom of=/root/scrapearc.key bs=512 count=8
sudo chmod 400 /root/scrapearc.key
sudo cryptsetup luksFormat --type luks2 /dev/sda3 /root/scrapearc.key
sudo cryptsetup luksAddKey --key-file /root/scrapearc.key /dev/sda3      # backup passphrase: write it down
sudo cryptsetup open --key-file /root/scrapearc.key /dev/sda3 scrapearc
sudo mkfs.ext4 -L scrapearc /dev/mapper/scrapearc
```
On Debian 13, `systemd-cryptsetup` is a separate package. Without it, `/etc/crypttab` is ignored at boot and the drive
silently stays locked. Check with `ls /usr/lib/systemd/system-generators/ | grep crypt`: it should print
`systemd-cryptsetup-generator`.

## Unlock and mount at boot

```
U=$(sudo blkid -s UUID -o value /dev/sda3)
echo "scrapearc UUID=$U /root/scrapearc.key luks,nofail" | sudo tee -a /etc/crypttab
sudo mkdir -p /mnt/scrapearc
echo "/dev/mapper/scrapearc /mnt/scrapearc ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
sudo systemctl daemon-reload
sudo mount /mnt/scrapearc
sudo mkdir /mnt/scrapearc/data
sudo chown $USER:$USER /mnt/scrapearc/data
```
Then make Docker wait for the drive. Without this, if the drive isn't unlocked, Docker would start anyway and the app
would write into an empty `/mnt/scrapearc` on the internal disk.
```
sudo mkdir -p /etc/systemd/system/docker.service.d
printf '[Unit]\nRequiresMountsFor=/mnt/scrapearc\n' | sudo tee /etc/systemd/system/docker.service.d/wait-for-drive.conf
sudo systemctl daemon-reload
```
In `.env.minibooru`, set `MINIBOORU_DATA_DIR=/mnt/scrapearc/data`.

## Back up the key and the LUKS header

The header holds the lock itself. If it gets damaged, the data is gone even with the key. On the server:
```
sudo cryptsetup luksHeaderBackup /dev/sda3 --header-backup-file ~/scrapearc-header.img
sudo cp /root/scrapearc.key ~/
sudo chown $USER:$USER ~/scrapearc.key ~/scrapearc-header.img
```
On your computer, copy one file per `scp` (quote paths that contain spaces):
```
mkdir -p ~/scrapearc-keys
scp user@server:scrapearc.key ~/scrapearc-keys/
scp user@server:scrapearc-header.img ~/scrapearc-keys/
```
Then run `rm ~/scrapearc.key ~/scrapearc-header.img` on the server. Keep a second copy of the keys (USB stick or
password manager), and never put them in a git repo.

To open the drive on another Linux machine (find the partition with `lsblk`):
```
sudo cryptsetup open --key-file scrapearc.key /dev/sdX3 scrapearc
sudo mkdir -p /mnt/scrapearc && sudo mount /dev/mapper/scrapearc /mnt/scrapearc
```

## Test it

Run `sudo reboot`. Afterwards, `df -h /mnt/scrapearc` must show `/dev/mapper/scrapearc`. If it shows the internal disk
instead, the drive didn't unlock.

To make the app refuse to start on an empty, not-yet-unlocked folder (instead of starting a new, empty archive
there), add `MINIBOORU_DATA_REQUIRED=1` to `.env.minibooru`. It then shows a "data is locked" page instead, writes
nothing, and stops after a minute so Docker restarts it and looks again. The archive is recognised by a
`.scrapearc-data` file the app puts in the data folder (don't delete it). An existing archive gets it automatically.

## Unlock with the TPM chip instead (no key file on the server)

Most PCs from about 2017 on have a TPM 2.0 security chip (check: `ls /dev/tpm0`). It can hold the key, so the drive
unlocks by itself at boot, also after a power cut, without a key file on the internal disk. Your passphrase keeps
working as a backup.

What it protects: someone who takes **only the drive** can't read it. Someone who takes **the whole computer** can,
because it unlocks itself when switched on. The chip refuses after a BIOS/firmware update or a Secure Boot change;
then unlock with the passphrase and add the chip again (last command below). Kernel and system updates don't affect it.
```
sudo apt install -y systemd-cryptsetup tpm2-tools
sudo cryptsetup luksHeaderBackup /dev/sda3 --header-backup-file /root/scrapearc-header-before-tpm.img
sudo systemd-cryptenroll --tpm2-device=auto --tpm2-pcrs=7 /dev/sda3      # asks for the passphrase
```
In `/etc/crypttab`, replace `/root/scrapearc.key` with `none` and add `tpm2-device=auto` to the options
(`scrapearc UUID=... none luks,nofail,tpm2-device=auto`), then `sudo systemctl daemon-reload` and test with
`sudo reboot`. Once that works, delete `/root/scrapearc.key` (keep your off-server copy).
If the chip ever refuses: unlock by hand (Troubleshooting below), then
`sudo systemd-cryptenroll --wipe-slot=tpm2 --tpm2-device=auto --tpm2-pcrs=7 /dev/sda3`.

## Troubleshooting

If `make status` hangs after a reboot, the drive didn't unlock, so Docker is waiting for it. Press Ctrl+C, then:

- Run `ls /usr/lib/systemd/system-generators/ | grep crypt`. If it prints nothing, run `sudo apt install systemd-cryptsetup`.
- Read the boot log: `sudo journalctl -b -u systemd-cryptsetup@scrapearc`.
- Unlock by hand: `sudo systemctl start systemd-cryptsetup@scrapearc`, then `sudo mount /mnt/scrapearc`, then
  `sudo systemctl start docker`.
