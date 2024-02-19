#!/bin/bash

# Check for operation mode presence
if [ "$#" -ne 1 ]; then
    echo "Usage: $0 <create|apply>"
    exit 1
fi

MODE=$1
ROOT_FOLDER="." # Assuming the script is run from the directory above original, mod, and sources

create_patches_and_sources() {
    echo "Creating patches and updating sources..."
    find "${ROOT_FOLDER}/mod" -type f -not -name ".*" | while read -r mod_file; do
        # Derive corresponding original and sources file paths
        relative_path="${mod_file#${ROOT_FOLDER}/mod/}"
        original_file="${ROOT_FOLDER}/original/${relative_path}"
        sources_file="${ROOT_FOLDER}/sources/${relative_path}.patch" # Use .patch for diff files

        if [ -e "$original_file" ]; then
            # Create patch if original file exists
            mkdir -p "$(dirname "$sources_file")"
            diff -u "$original_file" "$mod_file" > "$sources_file"
            echo "Patch created for: $relative_path"
        else
            # Copy mod file to sources if original does not exist
            mkdir -p "$(dirname "${sources_file%.patch}")"
            cp "$mod_file" "${sources_file%.patch}"
            echo "Mod file copied to sources: ${relative_path}"
        fi
    done
}

apply_patches_and_update_mod() {
    echo "Applying patches and updating mod from sources..."
    find "${ROOT_FOLDER}/sources" -type f | while read -r sources_file; do
        # Derive corresponding mod file path
        relative_path="${sources_file#${ROOT_FOLDER}/sources/}"
        relative_path="${relative_path%.patch}" # Remove .patch for non-patch files
        mod_file="${ROOT_FOLDER}/mod/${relative_path}"
        original_file="${ROOT_FOLDER}/original/${relative_path}"

        if [[ "$sources_file" == *.patch ]]; then
            # Apply patch if it's a patch file
            if [ -e "$original_file" ]; then
                mkdir -p "$(dirname "$mod_file")"
                patch "$original_file" "$sources_file" -o "$mod_file"
                echo "Applied patch to create: $relative_path"
            else
                echo "Original file missing for patch: ${relative_path}, cannot apply."
            fi
        else
            # Check if a corresponding file exists in original before copying
            if [ -e "$original_file" ]; then
                echo "Error: File $relative_path exists in original. Expected a patch file, not a full file in sources."
                exit 1
            elif [ ! -e "$mod_file" ]; then
                mkdir -p "$(dirname "$mod_file")"
                cp "$sources_file" "$mod_file"
                echo "Sources file copied to mod: $relative_path"
            fi
        fi
    done
}

# Main operation
case "$MODE" in
    create)
        create_patches_and_sources
        ;;
    apply)
        apply_patches_and_update_mod
        ;;
    *)
        echo "Invalid mode. Use 'create' to generate patches and update sources, or 'apply' to apply patches and update mod."
        exit 1
        ;;
esac
