#!/bin/bash

# Function to run a command remotely over SSH
run_remote_command() {
    local command_to_run="$1"

    # SSH into the remote machine and execute the command
    sshpass -p "$password" ssh -o StrictHostKeyChecking=accept-new "$username@$origin_ip" "$command_to_run"
}

# Function to get the size of a directory
get_directory_size() {
    local directory="$1"

    # Check if the directory exists
    if run_remote_command "[ -d \"$directory\" ]"; then
        # Calculate the size of the directory in bytes using du command
        local dir_size=$(run_remote_command "du -sb \"$directory\" | cut -f1")
        # Convert bytes to GB (1 GB = 1024 * 1024 * 1024 bytes)
        local dir_size_gb=$(echo "scale=2; $dir_size / (1024 * 1024 * 1024)" | bc)
        echo "$dir_size_gb"
    else
        echo "Error: Directory not found or invalid path."
    fi
}

# Fuction to ask whether the XDG directory should be copied over
get_copy_decision() {
    local directory="$1"
    # Get the directory path using xdg-user-dir
    local directory_path=$(run_remote_command "xdg-user-dir \"$directory\"")
    # Get the size of the directory
    local size_in_gb=$(get_directory_size "$directory_path")
    local directory_name=$(basename "$directory_path")
    # Ask the user if the directory should be included
    read -p "Copy over $directory_name? The size of the folder is ${size_in_gb}GB. ([y]/n): " answer
    answer=${answer:-y}

    # Return the user's answer
    echo "$answer"
}

# Function to copy the chosen XDG directory over
copy_xdg_dir() {
    local directory="$1"
    local answer="$2"
    local directory_path=$(xdg-user-dir "$directory")
    local directory_name=$(basename "$directory_path")
    
    if [[ "$answer" =~ ^[yY] ]]; then
        # Create the local directory if it doesn't exist
        mkdir -p "$directory_path"
        
        # Copy the directory from remote to local using rsync
        sshpass -p "$password" rsync -chazP --chown="$USER:$USER" --stats "$username@$origin_ip:/home/$username/$directory_name" "$HOME"
        echo "The $directory_name has been copied over." 
    fi
}

read -p "LINUX DESKTOP MIGRATION TOOL
This is a tool that helps with migration to a new computer. It has several preconditions:

- Both computers need to be on the same local network. You will need to know the IP address of the origin computer. You can find it out in the network settings.
- The origin computer needs to have remote login via ssh enabled. You can enable it in Settings/Sharing.
- The destination computer is expected to be freshly installed with the user set up. Any data at the destination computer may be overriden.
- Already installed flatpaks will be reinstalled from Flathub.

Press Enter to continue or Ctrl+C to quit.

"

read -p "Enter the origin IP address: " origin_ip

read -p "Enter the origin username [$USER]: " username
username=${username:-$USER}

echo -n "Enter the user password: "
read -s password
echo

# Asking about copying the XDG directories over
doc_answer=$(get_copy_decision "DOCUMENTS")

vid_answer=$(get_copy_decision "VIDEOS")

pic_answer=$(get_copy_decision "PICTURES")

mus_answer=$(get_copy_decision "MUSIC")

dwn_answer=$(get_copy_decision "DOWNLOAD")

echo

# Ask the user if they want to reinstall Flatpak applications
read -p "Do you want to reinstall Flatpak applications on the new machine? ([y]/n): " reinstall_answer
reinstall_answer=${reinstall_answer:-y}

# Ask the user if they want to copy the Flatpak app data over
if [[ "$reinstall_answer" =~ ^[yY] ]]; then
    read -p "Do you want to copy the Flatpak app data over, too? ([y]/n): " data_answer
    data_answer=${data_answer:-y}
fi

# Generate a list of installed flatpaks on the origin machine to reinstall on the destination machine
if [[ "$reinstall_answer" =~ ^[yY] ]]; then
    run_remote_command "flatpak list --app --columns=application" > installed_flatpaks.txt
    echo "List of installed Flatpaks saved to 'installed_flatpaks.txt'."
else
    echo "No action taken. Flatpak applications will not be reinstalled."
fi

# Ask the user if they want to migrate Toolbx containers
if command -v toolbox &>/dev/null; then
    # Run toolbox list command and store the output
    toolbox_list_output=$(run_remote_command "toolbox list")
    # Extract container IDs and names using awk and store them in an array
    IFS=$'\n' read -r -d '' -a container_ids_and_names <<< "$(echo "$toolbox_list_output" | awk '/^CONTAINER ID/{flag=1; next} flag && /^[a-f0-9]+/{print $1 "\t" $2}')"
    # If there are any Toolbx containers on the origin machine, ask whether to migrate them
    if [ "${#container_ids_and_names[@]}" -gt 0 ]; then
    read -p "You seem to be using Toolbx, would you like to migrate its containers? ([y]/n): " toolbx_answer
    toolbx_answer=${toolbx_answer:-y}
    fi
fi

echo

read -p "Press enter to start the migration. It will take some time. You can leave the computer, have a coffee and wait until the migration is finished.
"

# Copy home directories over
copy_xdg_dir "DOCUMENTS" "$doc_answer"
copy_xdg_dir "VIDEOS" "$vid_answer"
copy_xdg_dir "PICTURES" "$pic_answer"
copy_xdg_dir "MUSIC" "$mus_answer"
copy_xdg_dir "DOWNLOAD" "$dwn_answer"

#Reinstall flatpaks from the origin machine
if [[ "$reinstall_answer" =~ ^[yY] ]]; then
    xargs flatpak install -y --reinstall flathub < installed_flatpaks.txt
    echo "Flatpak applications have been reinstalled on the new machine."
fi

# Copy flatpak app data in ~/.var/app/ over from the origin machine
if [[ "$data_answer" =~ ^[yY] ]]; then
    echo "Now the flatpak app data will be copied over."
    mkdir -p "$HOME/.var/app"
    sshpass -p "$password" rsync -chazP --chown="$USER:$USER" "$username@$origin_ip:/home/$username/.var/app/" "$HOME/.var/app/"
fi

# Migrate Toolbx containers, loop through each container ID and name, save it as an image and export it to tar, copy it over and import it
if [[ "$toolbx_answer" =~ ^[yY] ]]; then
    for container_id_name in "${container_ids_and_names[@]}"; do
        container_id="${container_id_name%%$'\t'*}"
        container_name="${container_id_name#*$'\t'}"    
        if [ -n "$container_id" ] && [ -n "$container_name" ]; then
            # Stop the container remotely
            run_remote_command "podman container stop $container_id"
            # Create an image out of the container remotely
            run_remote_command "podman container commit $container_id $container_name-migrated"
            # Export the image as tar remotely
            run_remote_command "podman save -o $container_name.tar $container_name-migrated"
            # Move the exported tar file from remote to local using rsync
            sshpass -p "$password" rsync -chazP --remove-source-files --chown="$USER:$USER" --stats "$username@$origin_ip:$container_name.tar" .
            # Remove the exported image from local storage
            run_remote_command "podman rmi $container_name-migrated"
            # Load the image on the destination computer
            podman load -i "$container_name.tar"
            # Create a container from the imported image
            toolbox create --container "$container_name" --image "$container_name-migrated"
            # Delete the imported tar file
            rm "$container_name.tar"
        fi
    done
echo "Toolbx containers migrated.
"
fi

echo "
The migration is finished!"
