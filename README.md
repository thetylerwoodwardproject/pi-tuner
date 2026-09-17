# Pi Tuner

Stream over-the-air radio from a Raspberry Pi to an Icecast server, with
nothing more than a config file per station.

Pi Tuner takes cheap USB TV-tuner dongles (RTL-SDRs) and turns each one into an
internet radio station. Plug in a dongle, tune it to a frequency, and it
streams that station to your Icecast server where anyone on your network can
listen. No web interface, no database — just a folder of text config files and
one Python program that systemd keeps running.

**What it does**

- Tunes FM stations (stereo) and NOAA Weather Radio (mono).
- Streams each station to a local [Icecast](https://icecast.org/) server.
- Keeps every station running — if a dongle is unplugged or a stream dies, it
  restarts that station automatically.
- Optionally sends status and alerts to a [Zabbix](https://www.zabbix.com/)
  server.

## What you need

- A Raspberry Pi running Raspberry Pi OS (Debian).
- One RTL-SDR USB dongle per station you want to stream.
- An antenna for each dongle (a cheap telescopic antenna works for FM).

Everything else — the software, the Icecast server, the FM decoder — is
installed for you by the installer.

## Install (one line)

Run this on the Pi:

```sh
curl -fsSL https://raw.githubusercontent.com/thetylerwoodwardproject/pi-tuner/main/install.sh -o /tmp/pituner-install.sh && sudo bash /tmp/pituner-install.sh
```

The installer walks you through each step and asks before doing anything:
it installs packages, configures Icecast, guides you through programming your
dongle serials, and helps you set up your first station or two. You don't need
to set up every station now — you can add more any time.

> **Note:** the installer **resets your Icecast configuration** — it regenerates
> the source/admin passwords and restarts Icecast. If you already use Icecast
> for other streams, back it up first.

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
reliably — and it only takes a minute per dongle.

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
6. Plug them all back in and check `rtl_test` again — you should see one line
   per dongle, each with its own serial.

Notes:

- `rtl_eeprom` is installed as part of the `rtl-sdr` package (the installer
  installs it for you).
- Any 8-digit number works. Pick a scheme you can remember, e.g. `00001001`,
  `00001002`, …
- Pi Tuner matches serials by number, so `1001`, `0001001`, and `00001001` are
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
MOUNT=/tuner1
# ─── end user settings ─────────────────────────────
```

- To **add** a station: drop a new `.conf` file in `stations/`.
- To **change** a station: edit its file.
- To **remove** a station: delete its file.

After changing the files, tell the service to reload — no full restart needed:

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
| `FREQUENCY` | — (required)    | Frequency in MHz                                 |
| `SERIAL`    | —               | Dongle serial (matched by number)                |
| `GAIN`      | none (auto)     | Tuner gain in dB, e.g. `40.2`                    |
| `MOUNT`     | `/<file name>`  | Icecast mount path for this station              |

### `icecast.conf` (shared)

| Key               | Default     | Meaning                         |
|-------------------|-------------|---------------------------------|
| `HOST`            | `localhost` | Icecast host                    |
| `PORT`            | `8000`      | Icecast source port             |
| `SOURCE_PASSWORD` | `CHANGEME`  | Icecast `<source-password>`     |

The installer fills these in for you; you normally only touch `icecast.conf` if
you change your Icecast password later.

## Zabbix alerts (optional)

If you run a Zabbix server internally, Pi Tuner can push station events and a
status heartbeat to it (no agent needed on the Pi).

1. On your Zabbix server, import `zabbix_template.xml`
   (Configuration → Templates → Import).
2. Create a host (e.g. `pituner`) and attach the `Pi Tuner v2` template.
3. Edit `zabbix.conf` on the Pi:
   - `ENABLED=true`
   - `SERVER` = your Zabbix server
   - `HOSTNAME` = the host name you created in step 2
4. `sudo systemctl restart pituner.service`

Zabbix being unreachable never affects tuning — sends are best-effort and
logged.

## Troubleshooting

| Problem                          | Check                                              |
|----------------------------------|----------------------------------------------------|
| Station won't start              | `journalctl -u pituner -f`                         |
| Wrong/no dongle found            | `rtl_test` (check serials)                         |
| Config doesn't parse             | `tuner.py --dir /opt/pituner --check`              |
| No audio / stream missing        | `http://<pi>:8000/status-json.xsl` in a browser    |
| Changes didn't apply             | `sudo systemctl reload pituner.service`            |

The service automatically restarts any station whose pipeline dies (dongle
unplugged, Icecast unreachable, decode failure), backing off between attempts.

## Project layout

```
/opt/pituner/
  tuner.py            # the whole app (Python standard library only)
  icecast.conf        # shared Icecast connection
  zabbix.conf         # Zabbix trapper settings (optional)
  stations/           # one *.conf file per station
    fm-example.conf
    wx-example.conf
  zabbix_template.xml # import into Zabbix (optional)
  pituner.service     # systemd unit
```

## Uninstall

Remove the service, application files, the `pituner` user, and the `demux`
decoder (system packages are left in place):

```sh
sudo ./uninstall.sh          # asks before removing each thing
sudo ./uninstall.sh --yes    # remove everything without prompting
```

## License

See [LICENSE](LICENSE).
