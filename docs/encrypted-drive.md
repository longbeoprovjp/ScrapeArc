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

## Troubleshooting

If `make status` hangs after a reboot, the drive didn't unlock, so Docker is waiting for it. Press Ctrl+C, then:

- Run `ls /usr/lib/systemd/system-generators/ | grep crypt`. If it prints nothing, run `sudo apt install systemd-cryptsetup`.
- Read the boot log: `sudo journalctl -b -u systemd-cryptsetup@scrapearc`.
- Unlock by hand: `sudo systemctl start systemd-cryptsetup@scrapearc`, then `sudo mount /mnt/scrapearc`, then
  `sudo systemctl start docker`.
