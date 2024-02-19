#!/bin/bash

# Check for operation mode and root folder presence
if [ "$#" -ne 1 ]; then
    echo "Usage: $0 <create|apply>"
    exit 1
fi

MODE=$1
ROOT_FOLDER="." # Assuming the script is run from the directory above original, mod, and patch

create_patches() {
    echo "Creating patch files..."
    find "${ROOT_FOLDER}/original" -type f -name "*.txt" | while read -r original_file; do
        # Derive corresponding mod and patch file paths
        relative_path="${original_file#${ROOT_FOLDER}/original/}"
        mod_file="${ROOT_FOLDER}/mod/${relative_path}"
        patch_file="${ROOT_FOLDER}/patch/${relative_path}.patch"

        if [ -e "$mod_file" ]; then
            mkdir -p "$(dirname "$patch_file")"
            diff -u "$original_file" "$mod_file" > "$patch_file"
            echo "Patch created for: $relative_path"
        else
            echo "No corresponding mod file for $relative_path, skipping..."
        fi
    done
}

apply_patches() {
    echo "Applying patch files..."
    find "${ROOT_FOLDER}/patch" -type f -name "*.txt.patch" | while read -r patch_file; do
        # Derive corresponding original and mod file paths
        relative_path="${patch_file#${ROOT_FOLDER}/patch/}"
        relative_path="${relative_path%.patch}"
        original_file="${ROOT_FOLDER}/original/${relative_path}"
        mod_file="${ROOT_FOLDER}/mod/${relative_path}"

        if [ -e "$original_file" ]; then
            mkdir -p "$(dirname "$mod_file")"
            patch "$original_file" "$patch_file" -o "$mod_file"
            echo "Applied patch to create: $relative_path"
        else
            echo "No original file for patch ${relative_path}.patch, skipping..."
        fi
    done
}

# Main operation
case "$MODE" in
    create)
        create_patches
        ;;
    apply)
        apply_patches
        ;;
    *)
        echo "Invalid mode. Use 'create' to generate patches or 'apply' to apply them."
        exit 1
        ;;
esac
 
