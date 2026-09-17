# Pi Tuner v2

A minimal, config-file-driven multi-station SDR streamer for Raspberry Pi.
No web UI, no auth, no alert daemon — just a directory of station configs, one
Python file, and a systemd service. Each station is tuned from a USB RTL-SDR
dongle and streamed to a local Icecast server, with optional status/alert
pushing to a Zabbix server over the trapper protocol.

## Layout

```
/opt/pituner/
  tuner.py            # the whole app (stdlib only)
  icecast.conf        # shared Icecast connection
  zabbix.conf         # Zabbix trapper settings (optional)
  stations/           # one *.conf file per station
    fm-example.conf
    wx-example.conf
  zabbix_template.xml # import into Zabbix (optional)
  pituner.service     # systemd unit
```

## Dependencies

- `rtl-sdr` (`rtl_fm`, `rtl_test`)
- `ffmpeg` (with `libmp3lame`)
- `demux` (FM stereo demuxer/de-emphasis, as used by v1)
- `icecast2` (local stream server)
- Python 3 (standard library only — no pip packages)

## Install

The easiest way is the interactive installer, which installs all dependencies
(including building the `demux` FM stereo decoder), configures Icecast, programs
your dongle serials, writes your station configs, sets up Zabbix (optional),
and verifies everything is streaming:

```sh
sudo ./install.sh
```

It steps through every detail with prompts and sensible defaults.

### Manual install (reference)

```sh
sudo mkdir -p /opt/pituner/stations
sudo cp tuner.py icecast.conf zabbix.conf pituner.service /opt/pituner/
sudo cp zabbix_template.xml /opt/pituner/
sudo cp stations/*.conf /opt/pituner/stations/

sudo useradd --system --no-create-home --home /opt/pituner pituner
sudo usermod -aG plugdev pituner          # access to USB SDR dongles
sudo chown -R pituner:pituner /opt/pituner

sudo cp /opt/pituner/pituner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pituner.service
```

The `demux` FM stereo decoder must also be built once:

```sh
sudo apt-get install -y git build-essential meson ninja-build pkg-config libliquid-dev
git clone https://github.com/windytan/stereodemux.git
cd stereodemux && meson setup build && meson compile -C build
sudo install -m 755 build/demux /usr/local/bin/demux   # path may be build/src/demux
```

### Serial numbers

Each station needs a `SERIAL` matching a programmed dongle. Program each dongle
one at a time:

```sh
sudo rtl_eeprom -d 0 -s 00001001
```

then unplug/replug it. Verify with:

```sh
/opt/pituner/tuner.py --dir /opt/pituner --check
```

Serials are matched numerically, so `1001`, `0001001`, and `00001001` all
resolve to the same dongle.

## Config reference

### `icecast.conf` (shared)

| Key              | Default     | Description                          |
|------------------|-------------|--------------------------------------|
| `HOST`           | `localhost` | Icecast host                          |
| `PORT`           | `8000`      | Icecast source port                   |
| `SOURCE_PASSWORD`| `CHANGEME`  | Icecast `<source-password>`           |

### Per-station `stations/*.conf`

| Key         | Default            | Description                                    |
|-------------|--------------------|------------------------------------------------|
| `NAME`      | file name          | Stream name (also `-ice_name`)                 |
| `BAND`      | `fm`               | `fm` (stereo) or `wx` (NOAA weather, mono)     |
| `FREQUENCY` | — (required)       | Frequency in MHz                               |
| `SERIAL`    | —                  | Dongle serial (numeric match)                  |
| `GAIN`      | none (auto)        | rtl_fm gain in dB, e.g. `40.2`                 |
| `MOUNT`     | `/<filename>`      | Icecast mount path                             |

Adding or removing a station is just adding/removing a `*.conf` file; send
`SIGHUP` to reload without restarting the service:

```sh
sudo systemctl reload pituner.service    # or: sudo kill -HUP $(pgrep -f tuner.py)
```

The service restarts any station whose pipeline dies (dongle unplugged, Icecast
unreachable, decode failure) with exponential backoff.

## Zabbix (optional)

`tuner.py` pushes to a Zabbix **trapper** item (the `zabbix_sender` protocol,
port 10051) using a pure-Python client — no `zabbix_sender` or agent needed on
the Pi.

1. In Zabbix, **Import** `zabbix_template.xml` (Configuration → Templates → Import).
2. Create a host named `pituner` (or match `HOSTNAME` in `zabbix.conf`) and
   attach the `Pi Tuner v2` template.
3. Edit `zabbix.conf`:
   - `ENABLED=true`
   - `SERVER` = your Zabbix server
   - `PORT=10051`
   - `HOSTNAME` = the Zabbix host name from step 2
4. `sudo systemctl restart pituner.service`

Items pushed:

| Key                        | Type     | Meaning                                  |
|----------------------------|----------|------------------------------------------|
| `pituner.event`            | text     | last event (station up/down, etc.)       |
| `pituner.status`           | text     | JSON snapshot of per-station status      |
| `pituner.stations_active`  | unsigned | number of stations currently streaming   |
| `pituner.heartbeat`        | unsigned | liveness marker sent every `INTERVAL`    |

The template includes triggers for: heartbeat lost, station down, serial not
found, and zero stations streaming. Adjust their thresholds/severity to taste.

Zabbix being unreachable never affects tuning — sends are fire-and-forget and
logged on failure.
