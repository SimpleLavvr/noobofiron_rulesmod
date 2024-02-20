# Mod repo for noobs of iron

 Script requires the diff and patch program. Use WSL on windows.

# How files are stored

- original is an empty folder you should place HOI4 original files if you don't want to use the  `--original-folder` argument

# Script usage information

`script <create|apply|clear> [--no-overwrite-mod] [--overwrite-sources] [--original-folder <path>] [--mod-folder <path>] [--sources-folder <path>]"`

You should try to always use `--original-folder` to use your HOI4 location, in WSL its probably like `"/mnt/c/Program Files/Steam/SteamApps/common/Hearts of Iron IV"`

Try to use `--mod-folder` if possible, currently to play you need to put it in the local paradox mod folder ( `"~/.local/share/Paradox Interactive/Hearts of Iron IV/mod/"` on linux and `"/mnt/c/Users/yourusername/Documents/Paradox Interactive/Hearts of Iron IV/"` on WSL.

`--sources-folder` is useful if your're debugging. Btw by default sources patches and files are not overwiritten.

Also this script runs on bash and requires dos2unix,patch and diff to work.

 
## Build mod

- Put the events, common  folder (or whatever too) in 'original' folder
- run ./apply_patches apply
- get the files out of mod, and put this in the destination mod folder.

## Create patches from modified files
 
- Put the game files in original, then put your working mod (the edited files) in mod (same location)
- run ./apply_patches create (this will create a patch for existing files and copy non-existing ones)
- run ./apply_patches clear (please dont commit modded files)
- commit it
