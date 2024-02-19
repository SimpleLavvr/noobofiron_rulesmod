# Mod repo for noobs of iron

 Script requires the diff and patch program. Use WSL on windows.

## Build mod

- Put the events, common  folder (or whatever too) in 'original' folder
- run ./apply_patches apply
- get the files out of mod, and put this in the destination mod folder.

## Create patches from modified files
 
- Put the game files in original, then put your working mod (the edited files) in mod (same location)
- run ./apply_patches create (this will create a patch for existing files and copy non-existing ones)
- commit it
