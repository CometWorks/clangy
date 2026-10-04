# Developer resources

## Pulsar (players and developers)
- Releases (portable, extract into its own folder, the folder is cleaned on update): https://github.com/SpaceGT/Pulsar/releases/latest
- Windows installer that also sets up Steam: https://github.com/StarCpt/Pulsar-Installer
- Executables: `Legacy` runs SE1 on .NET Framework 4.8, `Interim` runs SE1 on .NET 10, `Modern` runs SE2 on .NET 10. `-h` lists the options. On Linux `Interim` and `Modern` run natively, no Wine needed. `Legacy` runs only on Windows or under Proton.
- Steam launch options: `[PulsarPath] %command% [Args]` on Windows and Linux. With Proton: `bash -c 'exec "${@:0:$#}" [PulsarPath] "${@:$#}" [Args]' %command%`
- Developer options: `-skipintro -nosplash -sources`. `-sources` enables the Sources dialog for local "dev" folders.

## Dedicated servers (SE1)
- Magnetar, the dedicated server plugin loader built on Pulsar: https://github.com/CometWorks/magnetar
- Quasar, a supervisor and web UI that runs several Magnetar servers on one host (Linux systemd or Windows): https://github.com/CometWorks/quasar

## SE1 plugins
- Templates, use "Use this template" on GitHub and follow the README:
  - Client only: https://github.com/CometWorks/client-plugin-template
  - Client and server (also for server-only plugins): https://github.com/CometWorks/server-plugin-template
- The templates' `AGENTS.md` has best practices, useful for humans too.
- Build and test locally either from the IDE (the build copies the DLL to `%AppData%\Pulsar\Legacy\Local`) or with a dev folder in Pulsar's Sources dialog. Test with a dev folder before every release, since Pulsar must be able to build the plugin itself.
- An `Interim` folder next to `Legacy` in the Pulsar folder keeps separate profiles for .NET 10 testing.
- Release: fill in the plugin's XML file and open a PR adding it to the `Plugins` folder of https://github.com/StarCpt/PluginHub . A human reviews the source, so it takes a while. Updates go the same way.
- Client-only workshop mods can be registered too: copy `SampleMod.xml` from the PluginHub root into `Plugins/Mods`, fill it in, test with a local hub, then open a PR. Check the mod isn't already registered first.
- Harmony (patching): https://harmony.pardeike.net/api/index.html
- Krafs Publicizer: https://github.com/krafs/Publicizer
- NuGet packages must target .NET Standard 2.0 (works on both runtimes) or .NET Framework 4.8 (Legacy only).
- Plugins can use up to C# 14 ("latest minor"). Mods are limited to C# 7.3 and programmable block scripts to C# 6.0.
- Asset files: implement `LoadAssets` in the `Plugin` class.
- All registered plugins are open source on GitHub, good examples to learn from.
- Native Linux Pulsar is the suggested way to develop on Linux, with full debugging.
- Useful plugin while developing: `Instant Exit`.

## AI assisted development
- Claude Code with the SE development skills: https://github.com/CometWorks/skills (SE1) and https://github.com/CometWorks/skills2 (SE2). The code skills decompile the game locally. Never publish decompiled game code.

## Discord
- Plugin ideas and questions have their own channels on this server.
