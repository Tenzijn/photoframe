# PhotoFrame

Turns an old Ubuntu laptop into a full-screen, shuffling photo gallery.

## Install (on the Ubuntu laptop)

Copy this folder to the laptop (USB stick, etc.), then:

```bash
cd photoframe
./install.sh
```

Open **PhotoFrame** from the app menu. The first time, the settings window opens so you can pick a folder.

## Controls

| Key | Action |
|---|---|
| `S`, `Enter` or right-click | Settings |
| `→` / `←` | Next / previous photo |
| `Space` | Pause / resume |
| `Esc` / `Q` | Quit |

## Settings

Photo folder · include subfolders · shuffle · seconds per photo · show whole photo or fill screen ·
blurred or black background · fade · clock/date · file name · keep screen on · start at login.

Saved to `~/.config/photoframe/config.json`. New photos added to the folder are picked up every 10 minutes.

## Keeping the laptop on

While PhotoFrame runs it blocks suspend (`systemd-inhibit`), turns off screen blanking and the
GNOME idle/sleep timers, and puts them all back when you quit.

If you want to **close the lid** and keep it running (e.g. with an external monitor), run once:

```bash
sudo sed -i 's/^#\?HandleLidSwitch=.*/HandleLidSwitch=ignore/' /etc/systemd/logind.conf
sudo systemctl restart systemd-logind
```

For a true photo-frame setup, also enable automatic login in
*Settings → Users*, and tick "Start automatically when I log in" in PhotoFrame.
