# FluidSynth Lazy Launcher

A small terminal launcher that makes it easier to play MIDI files with SoundFonts and render the result to WAV or MP3.

It is designed as a simple menu-driven frontend for [FluidSynth](https://www.fluidsynth.org/), without requiring a full DAW or graphical MIDI application.

## Features

- Choose and remember a MIDI folder
- Choose and remember a SoundFont folder
- Choose and remember an output folder
- Browse and filter MIDI files (`.mid`, `.midi`)
- Browse and filter SoundFonts (`.sf2`, `.sf3`)
- Play MIDI files through the selected SoundFont
- Stop playback from the launcher
- Render directly to WAV
- Render to MP3 with optional FFmpeg support
- Choose a random MIDI file
- Avoid overwriting existing rendered files
- No Python or pip packages required

## Requirements

### Required

- Bash 4 or newer
- FluidSynth

### Optional

- FFmpeg, only for MP3 export

Most Linux systems already include Bash and the standard command-line utilities used by the launcher.

### Debian / Ubuntu / Devuan

```bash
sudo apt install fluidsynth
```

For MP3 export:

```bash
sudo apt install ffmpeg
```

### Arch / Artix

```bash
sudo pacman -S fluidsynth
```

For MP3 export:

```bash
sudo pacman -S ffmpeg
```

### Fedora

```bash
sudo dnf install fluidsynth
```

Install FFmpeg separately if you want MP3 export and it is not already available on your system.

## Running

Download `fluidsynth-lazy-launcher`, then make it executable:

```bash
chmod +x fluidsynth-lazy-launcher
./fluidsynth-lazy-launcher
```

On first launch, enter three folders:

1. MIDI folder
2. SoundFont folder
3. Output folder

The launcher remembers these locations for future runs.

## Menu

```text
1. Choose MIDI
2. Choose SoundFont
3. Play selected MIDI
4. Stop playback
5. Render selected MIDI to WAV
6. Render selected MIDI to MP3
7. Choose a random MIDI
8. Change folders
9. Exit
```

## Configuration

Settings and the FluidSynth playback log are stored under:

```text
~/.config/fluidsynth-lazy-launcher/
```

If `XDG_CONFIG_HOME` is set, that location is used instead.

## MP3 export

MP3 export is intentionally optional. The launcher first uses FluidSynth to render the MIDI and SoundFont to temporary WAV audio, then FFmpeg converts that audio to MP3. The temporary WAV is removed afterward.

WAV export does not require FFmpeg.

## Version

Current version: **1.1.0**

Check from the terminal with:

```bash
./fluidsynth-lazy-launcher --version
```

## Scope

FluidSynth Lazy Launcher does not include MIDI files or SoundFonts. It is only a frontend for files supplied by the user.

## AI Disclosure

This project was developed with assistance from AI tools. AI was used to help generate, review, and refine portions of the code and documentation. The project was tested and reviewed by the repository owner before release.

## License

FluidSynth Lazy Launcher is licensed under the MIT License. See [LICENSE](LICENSE) for details.
