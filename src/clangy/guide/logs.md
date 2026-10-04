# Where the logs are

Pick the section for the player's setup. Pulsar overwrites `info.log` on every launch, so ask for it right after the problem happens.

Pulsar is portable: its folder is wherever the player put it. The default is `%AppData%\Pulsar` on Windows and `~/.config/Pulsar` on native Linux. If Pulsar was started with `-useHome`, it's that default folder even if the executable is elsewhere.

Under Proton, Windows paths live inside the game's Proton prefix: `%AppData%` is `steamapps/compatdata/<app id>/pfx/drive_c/users/steamuser/AppData/Roaming` in the Steam library that holds the game. SE1's app id is 244850.

With the `-stableLogs` Pulsar option the game overwrites one log file (for example `SpaceEngineers.log`) instead of creating timestamped ones.

## SE1 client

Pulsar's `info.log`:

| Executable | Log |
|---|---|
| `Legacy.exe` (Windows, Proton) | `<Pulsar folder>\Legacy\info.log` |
| `Interim.exe` (Windows, Proton) | `<Pulsar folder>\Legacy\info.log`, or `<Pulsar folder>\Interim\info.log` if the player created an `Interim` folder |
| `Interim.bin` (native Linux) | `<Pulsar folder>/Legacy/info.log`, or `<Pulsar folder>/Interim/info.log` if the player created an `Interim` folder |

Typical full paths:
- Windows: `%AppData%\Pulsar\Legacy\info.log`
- Native Linux: `~/.config/Pulsar/Legacy/info.log`

Game log, files named like `SpaceEngineers_20260919_034222834.log`, ask for the newest one:
- Windows and Proton: `%AppData%\SpaceEngineers\`
- Native Linux: `~/.config/SpaceEngineers/`

## SE2 client

Pulsar's `info.log`: `<Pulsar folder>/Modern/info.log`, typically `%AppData%\Pulsar\Modern\info.log` on Windows and `~/.config/Pulsar/Modern/info.log` on native Linux.

Game log, files named like `SpaceEngineers2_<date>_<time>_<pid>.log`, ask for the newest one:
- Native Linux: `~/.config/SpaceEngineers2/Temp/Logs/`
- Windows and Proton: in the `Temp\Logs` folder of SE2's app data folder. The exact path isn't confirmed, so if the player can't find it, ask them to search for `SpaceEngineers2_*.log`.

After an SE2 crash, Pulsar's crash dialog offers to open both the game log and Pulsar's log.

Crash dumps on native Linux: systemd keeps core dumps for a few days, `coredumpctl list Modern.bin` lists them. Only ask for one if a developer needs it.

## SE1 dedicated server (Magnetar)

Magnetar's `info.log` is in the `Magnetar` folder inside the Magnetar install folder, next to the launchers. `-config <dir>` moves it to that folder, `-useHome` to `%AppData%\Magnetar` (Windows) or `~/.config/Magnetar` (Linux). Unlike Pulsar, Magnetar keeps the previous launches as `info_<date>_<time>.log`, so ask for the one covering the problem. In Docker it's under the `/data/Magnetar` volume.

The game log is in the server's data folder, the one set by `-path`, next to `SpaceEngineers-Dedicated.cfg` and `Saves/`. Without `-path` it's `%AppData%\SpaceEngineersDedicated` on Windows and `~/.config/SpaceEngineersDedicated` on Linux. In Docker it's under the `/data/Instance` volume.

`MagnetarConfig` (the terminal UI shipped next to the launchers) can also show both logs.
