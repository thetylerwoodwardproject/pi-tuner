<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/logo-dark.svg">
    <img src="docs/images/logo-light.svg" alt="Pi-Tuner" width="380">
  </picture>
</p>

<p align="center">
  Turn cheap RTL-SDR dongles into internet radio stations.<br>
  A Raspberry Pi, one config file per station, and your own Icecast server.
</p>

<p align="center">
  <img alt="Status: in development" src="https://img.shields.io/badge/status-in%20development-ff9f0a?style=flat-square">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-26272b?style=flat-square">
  <img alt="Raspberry Pi OS" src="https://img.shields.io/badge/Raspberry%20Pi%20OS-Debian-26272b?style=flat-square">
  <img alt="Streams to Icecast" src="https://img.shields.io/badge/streams%20to-Icecast-26272b?style=flat-square">
  <a href="LICENSE"><img alt="MIT licence" src="https://img.shields.io/badge/licence-MIT-26272b?style=flat-square"></a>
</p>

<p align="center">
  <a href="#what-it-does">What it does</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#install-one-line">Install</a> ·
  <a href="#add-and-edit-stations">Stations</a> ·
  <a href="#rbds-now-playing-optional">RBDS</a> ·
  <a href="#configuration-reference">Config</a> ·
  <a href="#troubleshooting">Troubleshooting</a> ·
  <a href="#made-by">Made by</a>
</p>

> [!NOTE]
> **Pi-Tuner is a small, hobby-scale project.** The installer **resets your
> Icecast configuration** (it regenerates the source and admin passwords), so
> back up Icecast first if you already use it for other streams.

## What it does

Plug in a dongle, tune it to a frequency, and it streams that station to your
Icecast server where anyone on your network can listen. No web interface, no
database: just a folder of text config files and one Python program that
systemd keeps running.

<table>
  <tr>
    <td>📻&nbsp;<b>FM&nbsp;and&nbsp;weather&nbsp;radio</b></td>
    <td>Stereo FM, plus NOAA Weather Radio in mono</td>
  </tr>
  <tr>
    <td>🔊&nbsp;<b>Icecast&nbsp;streaming</b></td>
    <td>Every station is a 128k MP3 stream on its own mount: <code>/tuner1</code>, <code>/tuner2</code>…</td>
  </tr>
  <tr>
    <td>🏷️&nbsp;<b>RBDS&nbsp;now-playing</b></td>
    <td>Decodes RadioText and the station name and shows <code>Artist - Title (PS)</code> in Icecast. The station's program type (PTY) becomes the genre (<code>Radio</code> if none), and WX streams get <code>Weather</code></td>
  </tr>
  <tr>
    <td>⏺️&nbsp;<b>Recording</b></td>
    <td>Optionally saves any station to disk as 128&nbsp;kbps MP3 files, cut every 15 minutes into dated folders</td>
  </tr>
  <tr>
    <td>♻️&nbsp;<b>Self-healing</b></td>
    <td>If a dongle is unplugged or a stream dies, that station restarts automatically, with backoff</td>
  </tr>
  <tr>
    <td>🚨&nbsp;<b>EAS&nbsp;tone&nbsp;detection</b></td>
    <td>Listens for the 853 + 960 Hz attention tone and alerts you through Zabbix</td>
  </tr>
  <tr>
    <td>📟&nbsp;<b>Zabbix&nbsp;alerts</b></td>
    <td>Optional status heartbeat and events, with no agent on the Pi</td>
  </tr>
  <tr>
    <td>🧰&nbsp;<b>One-line&nbsp;installer</b></td>
    <td>Installs packages, builds the decoders, configures Icecast and walks you through your first stations</td>
  </tr>
</table>

## How it works

```mermaid
flowchart LR
    D[RTL-SDR dongle] --> R[rtl_fm]
    R --> T{tee}
    T --> S[demux<br>stereo + de-emphasis]
    S --> F[ffmpeg<br>MP3 128k]
    F --> I[(Icecast<br>/tunerN)]
    T -. RBDS stations .-> X[redsea]
    X --> M[rbds-meta]
    M -. now playing .-> I
```

Each station gets its own dongle, `rtl_fm` pipeline and Icecast mount. The
supervisor in `tuner.py` watches every pipeline and restarts any that die.

## What you need

- A Raspberry Pi running Raspberry Pi OS (Debian).
- One RTL-SDR USB dongle per station you want to stream.
- An antenna for each dongle (a cheap telescopic antenna works for FM).

Everything else (the software, the Icecast server, the FM and RBDS decoders) is
installed for you by the installer.

## Install (one line)

Run this on the Pi:

```sh
curl -fsSL https://raw.githubusercontent.com/thetylerwoodwardproject/pi-tuner/main/install.sh -o /tmp/pituner-install.sh && sudo bash /tmp/pituner-install.sh
```

The installer walks you through each step and asks before doing anything:
it installs packages, configures Icecast, guides you through programming your
dongle serials, and helps you set up your first station or two. You don't need
to set up every station now: you can add more any time.

> [!WARNING]
> The installer **resets your Icecast configuration**: it regenerates the
> source and admin passwords and restarts Icecast. Back up first if you already
> use Icecast for other streams.

Prefer to look at the code first? Clone and run instead:

```sh
git clone https://github.com/thetylerwoodwardproject/pi-tuner.git
cd pi-tuner
sudo ./install.sh
```

## Give each dongle a unique serial number

This step matters because **every RTL-SDR dongle ships with the same default
serial number** (usually `00000001`). With two or more plugged in, the Pi can't
tell them apart. Giving each dongle its own serial is what makes them work
reliably: and it only takes a minute per dongle.

Do this **one dongle at a time**:

1. Unplug all dongles, then plug in just **one**.
2. Write a new serial to it:
   ```sh
   sudo rtl_eeprom -d 0 -s 00001001
   ```
3. Unplug it, then plug it back in (the new serial only takes effect after a
   replug).
4. Confirm it worked:
   ```sh
   rtl_test
   ```
   Look for a line like `SN: 00001001`.
5. Repeat for each dongle with a different number: `00001002`, `00001003`, and
   so on.
6. Plug them all back in and check `rtl_test` again: you should see one line
   per dongle, each with its own serial.

Notes:

- `rtl_eeprom` is installed as part of the `rtl-sdr` package (the installer
  installs it for you).
- Any 8-digit number works. Pick a scheme you can remember, e.g. `00001001`,
  `00001002`, …
- Pi-Tuner matches serials by number, so `1001`, `0001001`, and `00001001` are
  all treated as the same dongle.

## Add and edit stations

Every station is one small text file in `stations/`:

```
# ─── user settings ─────────────────────────────────
NAME=My Station
BAND=fm            # fm | wx
FREQUENCY=98.1
SERIAL=00001001
GAIN=40.2          # dB; delete this line for auto-gain
RBDS=true          # optional, fm only; RBDS text -> Icecast now-playing
RECORD=true        # optional; save 15-minute MP3 recordings
# ─── end user settings ─────────────────────────────
```

- To **add** a station: drop a new `.conf` file in `stations/`.
- To **change** a station: edit its file.
- To **remove** a station: delete its file.

After changing the files, tell the service to reload: no full restart needed:

```sh
sudo systemctl reload pituner.service
```

Each station's stream is then available at:

```
http://<raspberry-pi-ip>:8000/<mount>
```

## Configuration reference

### Per-station file (`stations/*.conf`)

| Key         | Default         | Meaning                                          |
|-------------|-----------------|--------------------------------------------------|
| `NAME`      | the file name   | Station name shown in your player                |
| `BAND`      | `fm`            | `fm` (stereo) or `wx` (NOAA weather, mono)       |
| `FREQUENCY` |: (required)    | Frequency in MHz (FM 88–108, WX 162.400–162.550) |
| `SERIAL`    |:               | Dongle serial (matched by number)                |
| `GAIN`      | none (auto)     | Tuner gain in dB, e.g. `40.2`                    |
| `RBDS`      | `false`         | FM only: send RBDS text and PTY genre to Icecast |
| `RECORD`    | `false`         | Save this station to 15-minute MP3 files         |
| `RECORD_KEEP_DAYS` | keep all | Delete recordings older than this many days      |
| `MOUNT`     | `/tuner1`, `/tuner2`, … | Icecast mount, numbered in file order    |

### `icecast.conf` (shared)

| Key               | Default     | Meaning                                          |
|-------------------|-------------|--------------------------------------------------|
| `HOST`            | `localhost` | Icecast host                                     |
| `PORT`            | `8000`      | Icecast source port                              |
| `SOURCE_PASSWORD` | `CHANGEME`  | Icecast `<source-password>`                      |
| `ADMIN_USER`      | `admin`     | Optional: admin login for now-playing updates    |
| `ADMIN_PASSWORD`  | none        | Optional: if set, used instead of the source login |

The installer fills these in for you; you normally only touch `icecast.conf` if
you change your Icecast password later.

## Zabbix alerts (optional)

If you run a Zabbix server internally, Pi-Tuner can push station events and a
status heartbeat to it (no agent needed on the Pi).

1. On your Zabbix server, import `zabbix_template.xml`
   (Configuration → Templates → Import).
2. Create a host (e.g. `pituner`) and attach the `Pi-Tuner` template.
3. Edit `zabbix.conf` on the Pi:
   - `ENABLED=true`
   - `SERVER` = your Zabbix server
   - `HOSTNAME` = the host name you created in step 2
4. `sudo systemctl restart pituner.service`

Zabbix being unreachable never affects tuning: sends are best-effort and
logged.

## EAS attention-tone detection (optional)

If Zabbix is enabled, the Pi can listen for the EAS/SAME **attention signal**:
a simultaneous 853 Hz + 960 Hz dual-tone broadcast on FM and NOAA WX before
emergency messages, and alert you when it's heard.

Set `EAS_DETECT=true` in `zabbix.conf` (the default). When the tone is detected
on any FM/WX station, the Pi pushes `pituner.eas = 1` to Zabbix, holds it for
~10 seconds, then resets it to `0`, so a trigger on `last()=1` fires and then
auto-recovers. The station name is logged in `pituner.event`.

Detection is tone-only (it does not decode the SAME data). The thresholds are
constants at the top of `tuner.py` (`EAS_TONE_RATIO`, `EAS_HOLD_SECS`,
`EAS_COOLDOWN_SECS`) if you need to tune them for your signal levels.

## RBDS now-playing (optional)

Most US FM stations broadcast **RBDS** (the North American flavor of RDS), a
tiny data stream hidden in the FM signal. It carries the station's 8-character
**PS** name (e.g. `KXYZ-FM`) and a **RadioText (RT)** line, which is usually the
current artist and title. Pi-Tuner can decode it and show it as the Icecast
"now playing" text, so your player displays something like:

```
Artist - Title (KXYZ-FM)
```

Turn it on per FM station by adding `RBDS=true` to its file (the installer asks
you), then reload:

```sh
sudo systemctl reload pituner.service
```

How it works:

```
rtl_fm ──┬──▶ demux ──▶ ffmpeg ──▶ Icecast  (audio)
         └──▶ redsea ──▶ tuner.py rbds-meta ──▶ Icecast /admin/metadata  (now playing)
```

- The raw 192 kHz FM signal is split before stereo decoding. One copy goes to the
  audio path as usual; the other goes to
  [redsea](https://github.com/windytan/redsea), which decodes RBDS and outputs
  JSON.
- `tuner.py rbds-meta` combines the latest RT and PS into `RT (PS)` and updates
  the stream's metadata on its mount (`/tuner1`, `/tuner2`, …). If a station
  sends only RT or only PS, that one is shown by itself. Only changes are sent.
- The `(PS)` after the text is the station's PS name. Some stations scroll their
  PS ("Station", "Z93 The", "#1 Hit", "Music"), which isn't a name. When Pi-Tuner
  sees the PS change several times in a minute, it shows the callsign worked out
  from the station's PI code instead (or nothing if there isn't one), so the
  now-playing text doesn't flip every few seconds. The first update after a
  start waits about 12 seconds while it tells the two apart.
- The Icecast stream **name** is not changed: it stays the `NAME` from the
  station file. (Icecast only sets the name when a source connects.)
- Updates use the `source` login and `SOURCE_PASSWORD` from `icecast.conf`. To use
  the admin login instead, set `ADMIN_USER` and `ADMIN_PASSWORD` there.
- The Icecast **genre** is the station's RBDS **PTY** (program type), such as
  `Country`, `Top 40` or `Classic rock`. Icecast only reads the genre when a
  stream connects, so Pi-Tuner listens for the PTY for up to 8 seconds just
  before it starts each RBDS station. That adds a short delay at startup and
  after a reload, and a PTY that changes later is picked up at the next restart.
  If no PTY is heard, or the station sends "No PTY", the genre is `Radio`. FM
  stations without `RBDS=true` are `Radio` too.
- WX stations have no RBDS, and `RBDS=true` is ignored for the now-playing text.
  Their genre is always `Weather`, with or without `RBDS`.

To check it's working, open `http://<pi>:8000/status-json.xsl` (look for `title`
and `genre` on the mount) or watch `tail -f /var/www/pituner/rbds.log`.

Notes:

- RBDS needs a clean signal. Weak or noisy stations may decode slowly or not at
  all; try adjusting `GAIN` and your antenna.
- Some stations send only a PS name and no RadioText, or send ads and station
  slogans in RT instead of song info. Pi-Tuner shows whatever the station sends.
- `redsea` is built from source by the installer. If it isn't installed, a
  station with `RBDS=true` still streams audio normally and a warning is logged;
  RBDS is just skipped.
- Like the EAS detector, the RBDS helper is best-effort and can never interrupt
  the audio.

## Recording (optional)

Pi-Tuner can keep a log of what a station broadcast. Add `RECORD=true` to a
station's file (the installer asks you), then reload. The audio is saved as
**128 kbps MP3**, the same format as the Icecast stream, in **15-minute files**:

```
/opt/pituner/recordings/STATION_NAME/YYYY/MM/DD/YYMMDD_HHMMSS_STATION_NAME.mp3
```

For example:

```
/opt/pituner/recordings/WXYZ-FM/2026/03/05/260305_140000_WXYZ-FM.mp3
/opt/pituner/recordings/WXYZ-FM/2026/03/05/260305_141500_WXYZ-FM.mp3
```

- **HHMMSS is when that file began**, in the Pi's local time (check it with
  `timedatectl`).
- Files are cut on the clock, at :00, :15, :30 and :45. The first file after
  Pi-Tuner starts, reloads or restarts a station is shorter, and its name shows
  the time it actually began.
- Spaces in a station name become underscores in the folder and file names.
- Recording runs beside the stream and can't interrupt it. If the disk fills or
  the encoder fails, the problem is logged to `recordings.log` and the stream
  keeps playing. WX stations record the same mono audio that they stream.
- **RBDS.log (FM only).** If a station has both `RBDS=true` and `RECORD=true`,
  Pi-Tuner also keeps a text log of the now-playing text in that day's folder:

  ```
  /opt/pituner/recordings/STATION_NAME/YYYY/MM/DD/RBDS.log
  ```

  Every change is logged, in the Pi's local time, with a new file each day.
  The line depends on what the station sends:

  ```
  261001 09:41:07: WPR Music (WLSU)
  261001 09:44:52: Artist: Metallica, Title: Enter Sandman, PS: KQYZ-FM
  261001 09:46:10 RT: Avenged Sevenfold - Bat Country, Artist: Avenged Sevenfold, Title: Bat Country, PS: KQYZ-FM
  ```

  - **RadioText only:** `text (PS)`.
  - **RadioText Plus only:** RT+ splits the text into `Artist:` and `Title:`
    fields.
  - **Both:** when the RadioText and its RT+ change within about 5 seconds of each
    other, they share one line. It starts `RT:` with no colon after the time, so
    you can see exactly what a receiver displayed. The time is when the first of
    the two arrived.
  - A repeat of the text that's already showing is not logged again, but a text
    coming back after something else (a station rotating song and slogan) is.
  - If a station's RT+ is blank or has no song tags, you just get the plain
    RadioText lines.
  - `PS` is the station's PS name. If a station scrolls its PS, the callsign
    worked out from its PI code is shown instead, or nothing.
  - WX stations don't have an `RBDS.log`.
- **Plan for disk space.** Each station uses about 1.4 GB a day (58 MB an hour).
  Set `RECORD_KEEP_DAYS=14` in the station file to delete files (and `RBDS.log`s)
  older than 14 days, plus any empty date folders. Pi-Tuner checks hourly. Leave it out to
  keep everything.

## Troubleshooting

| Problem                          | Check                                              |
|----------------------------------|----------------------------------------------------|
| Station won't start              | `journalctl -u pituner -f`                         |
| Wrong/no dongle found            | `rtl_test` (check serials)                         |
| Config doesn't parse             | `tuner.py --dir /opt/pituner --check`              |
| No audio / stream missing        | `http://<pi>:8000/status-json.xsl` in a browser    |
| Changes didn't apply             | `sudo systemctl reload pituner.service`            |
| No now-playing text              | `which redsea`, then `tail /var/www/pituner/rbds.log` |
| FM genre is just `Radio`         | No PTY heard; check `journalctl -u pituner` for `RBDS PTY:` |
| No recordings                    | `RECORD=true` set and reloaded? `tail /var/www/pituner/recordings.log`; `df -h` |
| No `RBDS.log`                    | FM station with both `RBDS=true` and `RECORD=true`? Is `redsea` installed? |

The service automatically restarts any station whose pipeline dies (dongle
unplugged, Icecast unreachable, decode failure), backing off between attempts.

## Local logs

The Pi also keeps rotating logs on disk under `/var/www/pituner/`:

| File         | Contents                                                             |
|--------------|----------------------------------------------------------------------|
| `tuner.log`  | Tuner health: station status changes, restarts, reloads             |
| `eas.log`    | EAS attention-tone detections                                        |
| `zabbix.log` | Mirror of what's sent to Zabbix (events only, not heartbeats)        |
| `rbds.log`   | RBDS now-playing updates sent to Icecast, and any update failures    |
| `recordings.log` | Recording problems (disk full, encoder errors)                   |

They rotate by size (10 MB, keeping 5 compressed copies) via
`/etc/logrotate.d/pituner`. View them with e.g. `tail -f /var/www/pituner/tuner.log`.

## Project layout

```
/opt/pituner/
  tuner.py            # the whole app (Python standard library only)
  icecast.conf        # shared Icecast connection
  zabbix.conf         # Zabbix trapper settings (optional)
  stations/           # one *.conf file per station
  recordings/         # 15-minute MP3 recordings (when RECORD=true)
    fm-example.conf
    wx-example.conf
  zabbix_template.xml # import into Zabbix (optional)
  pituner.service     # systemd unit
```

The repository also has `install.sh` / `uninstall.sh`, `logrotate.conf`, a
`tests/` folder (`python3 -m unittest discover -s tests`) and the `docs/images`
used by this README. The installer additionally places the `demux` and `redsea`
decoders in `/usr/local/bin`.

## Uninstall

Remove the service, application files, the `pituner` user, and the `demux`
and `redsea` decoders (system packages are left in place):

```sh
sudo ./uninstall.sh          # asks before removing each thing
sudo ./uninstall.sh --yes    # remove everything without prompting
```

## Made by

<table>
  <tr>
    <td valign="middle">
      Pi-Tuner is made by <b>Tyler Woodward</b>, host of the podcast <a href="https://tylerwoodward.me"><b>The Tyler Woodward Project</b></a>.
      <br><br>
      <a href="https://tylerwoodward.me"><img alt="The Tyler Woodward Project" src="https://img.shields.io/badge/podcast-tylerwoodward.me-ff453a?style=flat-square"></a>
      <a href="https://www.facebook.com/thetylerwoodwardproject"><img alt="Facebook" src="https://img.shields.io/badge/Facebook-thetylerwoodwardproject-26272b?style=flat-square&logo=facebook&logoColor=white"></a>
      <a href="https://www.threads.net/@tylerwoodward.me"><img alt="Threads" src="https://img.shields.io/badge/Threads-@tylerwoodward.me-26272b?style=flat-square&logo=threads&logoColor=white"></a>
      <a href="https://www.instagram.com/tylerwoodward.me"><img alt="Instagram" src="https://img.shields.io/badge/Instagram-@tylerwoodward.me-26272b?style=flat-square&logo=instagram&logoColor=white"></a>
    </td>
  </tr>
</table>

## Credits

Pi-Tuner is built on [rtl-sdr](https://osmocom.org/projects/rtl-sdr/wiki),
[FFmpeg](https://ffmpeg.org) and [Icecast](https://icecast.org). Stereo decoding
uses [stereodemux](https://github.com/windytan/stereodemux) and RBDS decoding
uses [redsea](https://github.com/windytan/redsea), both by Oona Räsänen
(windytan).

## Licence

MIT © 2026 [Tyler Woodward](https://tylerwoodward.me). See [LICENSE](LICENSE).
