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
| `Esc` / `Q` / `Ctrl+Q` | Quit (always works) |

## Settings

Photo folder · include subfolders · shuffle · seconds per photo · show whole photo or fill screen ·
blurred or black background · fade · clock/date · file name · keep screen on · start at login.

Changes show on the slideshow behind the settings window straight away and are saved
automatically to `~/.config/photoframe/config.json`. The folder box checks your folder as you
type; a folder without photos never replaces the one that's playing. New photos added to the
folder are picked up every 10 minutes.

## Keeping the laptop on

While PhotoFrame runs it asks GNOME not to blank the screen or suspend. The system drops
that request by itself the moment PhotoFrame exits, even if it crashes, so **no settings are
changed permanently**. On battery, sleep is allowed again below 20% so the laptop is never
drained flat (a hard power-off is what damages files).

## Safety guard rails

- Memory is capped for PhotoFrame only; huge photos are skipped instead of freezing the laptop.
- Runs at low priority; if memory ever runs out, PhotoFrame is killed first, not your desktop.
- Only one copy runs at a time; it restarts itself if memory use creeps up over days.
- Never grabs the keyboard or mouse: `Esc` on the slideshow always quits.
- Logs go to `~/.cache/photoframe/photoframe.log` (and `crash.log` for hard crashes).
- Settings left behind by older versions of PhotoFrame are restored on first start.

If you want to **close the lid** and keep it running (e.g. with an external monitor), run once,
then **reboot** (don't restart `systemd-logind` while logged in, as that kills your desktop session):

```bash
sudo mkdir -p /etc/systemd/logind.conf.d
printf '[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\n' \
  | sudo tee /etc/systemd/logind.conf.d/photoframe-lid.conf
```

Undo with `sudo rm /etc/systemd/logind.conf.d/photoframe-lid.conf` and reboot. Don't put the
laptop in a bag with the lid ignored: it will keep running and get hot.

For a true photo-frame setup, also enable automatic login in
*Settings → Users*, and tick "Start automatically when I log in" in PhotoFrame.
