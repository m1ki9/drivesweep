<p align="center"><img src="cleaner/ui/logo.png" width="96" alt=""></p>

<h1 align="center">DriveSweep</h1>

<p align="center">A Windows app that finds what's filling your drives and actually frees the space.</p>

## Download

1. Go to [**Releases**](https://github.com/m1ki9/drivesweep/releases/latest) and download `DriveSweep.exe`.
2. Double-click it and allow the administrator prompt.
   Windows may first say *"Windows protected your PC"* because the app isn't code-signed:
   click **More info → Run anyway**.
3. That's it. The first run puts **DriveSweep** on your Desktop and in the Start menu, so you can
   delete the downloaded file. Downloading a newer version and running it once updates the app.

Needs Windows 10 or 11 (it uses the Edge WebView2 that comes with Windows).
To remove DriveSweep, delete its shortcuts and the folder `%LOCALAPPDATA%\Programs\DriveSweep`.

## What it does

When it opens, DriveSweep scans all your drives in the background, so by the time you pick one the
results are ready. For each drive you get four tabs:

- **Recommended**: everything worth deleting, grouped and sized, each with the reason and when it was
  last opened or changed. Safe items (caches, temp files, crash dumps, old logs, Windows upgrade leftovers)
  are pre-selected; everything else is marked *Review first*:
  - games and programs nothing uses anymore (old game folders, old Steam/Riot installs, portable apps)
  - installers, game repacks, ISOs and archives you don't need once something is installed
  - files Steam leaves behind after you uninstall a game, and stale downloads
  - leftovers from programs you uninstalled, duplicate files, big files you haven't opened in months
  - old photos, videos and documents, shown separately so they're never mixed up with junk
- **Apps**: installed programs and Microsoft Store apps with their icon, size and when they were last opened.
  - **Delete** runs the app's own uninstaller, waits for it to really finish, then offers to delete the
    folders it left behind (AppData, ProgramData, Program Files).
  - **Move** puts an app on another drive: its program folder and data folders are copied to
    `X:\Moved apps\<app>`, checked, then removed here. A link is left in the old place, so the app,
    its shortcuts and its uninstaller keep working exactly as before.
- **Cache & temp**: Windows temp and update files, browser, Discord, Spotify, GPU and developer caches,
  and the Recycle Bin.
- **Explore folders**: a tree of the whole drive, biggest first. Click a folder to expand it; colored bars
  show how much of its parent folder each item takes.

Deleting is **permanent** (not the Recycle Bin), always after a confirmation that lists everything and its size.

## How it decides a folder is unused

A folder is only suggested as an unused game or program when **all** of these hold (`cleaner/unused.py`):

1. **Nothing points at it.** No installed program, game launcher (Steam, Epic and Riot install records),
   Windows service, autostart entry or running program uses it. Links are followed, so moved apps still count.
2. **Nothing in it was opened recently.** Windows keeps a launch record for every program it starts
   (`C:\Windows\Prefetch`) with the program's full path and last run time. DriveSweep decodes these and
   knows when anything inside a folder was last opened. This needs admin; without it the rule is
   "no shortcut, and nothing changed for a year".
3. **Nothing big inside changed recently.** Games rewrite small configs and logs even when you don't play
   them, so only files of 1 MB and up count as a real change.
4. **It isn't your stuff.** Folders that are 30%+ photos, videos, music or documents are never called
   leftovers.

The topmost unused folder is reported, so an old games folder is one item, not fifty.

## License

DriveSweep is free and open source under the [MIT License](LICENSE).
