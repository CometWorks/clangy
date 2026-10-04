# Where the logs are

Pulsar is portable: its folder is wherever the player put it. The default is `%AppData%\Pulsar` on Windows and `~/.config/Pulsar` on Linux. Pulsar overwrites `info.log` on every launch, so ask for it right after the problem happens.

## Pulsar's `info.log`

| Game and runtime | Executable | Log |
|---|---|---|
| SE1, Legacy (.NET Framework 4.8, Windows or Proton) | `Legacy.exe` | `<Pulsar folder>\Legacy\info.log` |
| SE1, Interim (.NET 10, Windows or native Linux) | `Interim.exe` / `Interim.bin` | `<Pulsar folder>/Legacy/info.log`, or `<Pulsar folder>/Interim/info.log` if the player created an `Interim` folder |
| SE2, Modern (.NET 10, Windows or native Linux) | `Modern.exe` / `Modern.bin` | `<Pulsar folder>/Modern/info.log` |

Typical full paths:
- Windows, SE1: `%AppData%\Pulsar\Legacy\info.log`
- Native Linux, SE1: `~/.config/Pulsar/Legacy/info.log`
- Native Linux, SE2: `~/.config/Pulsar/Modern/info.log`

If Pulsar was started with `-useHome`, the folder is `%AppData%\Pulsar` (`~/.config/Pulsar`) even if the executable is elsewhere.

## Game logs

- SE1 on Windows (and inside Proton): `%AppData%\SpaceEngineers\`, files named like `SpaceEngineers_20260919_034222834.log`. Ask for the newest one.
- SE1 on native Linux: `~/.config/SpaceEngineers/`, same file names.
- SE2 on native Linux: `~/.config/SpaceEngineers2/Temp/Logs/`, files named like `SpaceEngineers2_<date>_<time>_<pid>.log`. Ask for the newest one.
- SE2 on Windows: in the `Temp\Logs` folder of SE2's app data folder. The exact path isn't confirmed, so if the player can't find it, ask them to search for `SpaceEngineers2_*.log`.

With the `-stableLogs` Pulsar option the game overwrites one log file (for example `SpaceEngineers.log`) instead of creating timestamped ones.

After an SE2 crash, Pulsar's crash dialog offers to open both the game log and Pulsar's log.

## Crash dumps

- SE2 on native Linux: systemd keeps core dumps for a few days, `coredumpctl list Modern.bin` lists them. Only ask for one if a developer needs it.
