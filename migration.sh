#!/bin/bash

#Copyright 2023 Jiri Eischmann
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/>. 

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
        local dir_size
        dir_size=$(run_remote_command "du -sb \"$directory\" | cut -f1")

        # Convert bytes to GB (1 GB = 1024 * 1024 * 1024 bytes)
        local dir_size_gb
        dir_size_gb=$(echo "scale=2; $dir_size / (1024 * 1024 * 1024)" | bc)
        echo "$dir_size_gb"
    else
        echo "Error: Directory not found or invalid path."
    fi
}

# Fuction to ask whether the XDG directory should be copied over
get_copy_decision() {
    local directory="$1"

    # Get the directory path using xdg-user-dir
    local directory_path
    directory_path=$(run_remote_command "xdg-user-dir \"$directory\"")

    # Get the size of the directory
    local size_in_gb
    size_in_gb=$(get_directory_size "$directory_path")

    local directory_name
    directory_name=$(basename "$directory_path")

    # Ask the user if the directory should be included
    IFS= read -p "Copy over $directory_name? The size of the folder is ${size_in_gb}GB. ([y]/n): " -r answer
    answer=${answer:-y}

    # Return the user's answer
    echo "$answer"
}

# Function to copy the chosen XDG directory over
copy_xdg_dir() {
    local directory="$1"
    local answer="$2"

    local directory_path_destination
    directory_path_destination=$(xdg-user-dir "$directory")

    local directory_name_destination
    directory_name_destination=$(basename "$directory_path_destination")

    local directory_path_origin
    directory_path_origin=$(run_remote_command "xdg-user-dir \"$directory\"")

    if [[ "$answer" =~ ^[yY] ]]; then
        # Create the local directory if it doesn't exist
        mkdir -p "$directory_path_destination"
        
        # Copy the directory from remote to local using rsync
        sshpass -p "$password" rsync -chazP --chown="$USER:$USER" --stats "$username@$origin_ip:$directory_path_origin/" "$directory_path_destination"
        echo "The $directory_name_destination has been copied over."
    fi
}

IFS= read -p "LINUX DESKTOP MIGRATION TOOL
This is a tool that helps with migration to a new computer. It has several preconditions:

- Both computers need to be on the same local network. You will need to know the IP address of the origin computer. You can find it out in the network settings.
- The origin computer needs to have remote login via ssh enabled. You can enable it in Settings/Sharing.
- The destination computer is expected to be freshly installed with the user set up. Any data at the destination computer may be overriden.

Press Enter to continue or Ctrl+C to quit.

" -r

while true; do
    IFS= read -p "Enter the origin IP address: " -r origin_ip
    IFS= read -p "Enter the origin username [$USER]: " -r username
    username=${username:-$USER}
    echo -n "Enter the user password: "
    IFS= read -rs password
    echo

    # Check the SSH connection
    if sshpass -p "$password" ssh -o StrictHostKeyChecking=accept-new -q $username@$origin_ip exit; then
        echo "The connection has been successfully established."
        break  # Break the loop if the connection is successful
    else
        echo "The connection with the origin computer could not be established. Please check the login information and make sure remote login is enabled on the origin computer.
        "
    fi
done
echo

# Asking whether to copy files in home directories
IFS= read -p "Would you like to migrate files in home directories? ([y]/n): " -r copy_answer
copy_answer=${copy_answer:-y}

# Get the origin user home directory path
user_home_origin=$(run_remote_command "eval echo \"~$username\"")

if [[ "$copy_answer" =~ ^[yY] ]]; then
    # Asking about copying the XDG directories over
    doc_answer=$(get_copy_decision "DOCUMENTS")

    vid_answer=$(get_copy_decision "VIDEOS")

    pic_answer=$(get_copy_decision "PICTURES")

    mus_answer=$(get_copy_decision "MUSIC")

    dwn_answer=$(get_copy_decision "DOWNLOAD")
    echo
    echo "Now you can pick other directories in home you would also like to copy over. Instead of full path, use path relative to the home directory. So instead of /home/user/example_dir just example_dir."
    
    # Asking for arbitrary home directories to copy over
    while true; do
        IFS= read -p "Enter the directory path (relative to your home directory) to copy (or 'done' to finish): " -r relative_dir
        if [ "$relative_dir" == "done" ]; then
            break
        else
            if [ "$relative_dir" == ".ssh" ]; then
                echo "Migrating the ~/.ssh directory is not currently supported. Skipping."
            else
                # Construct the full path by appending the relative path to the user's home directory
                dir="$user_home_origin/$relative_dir"
            
                if run_remote_command "test -d '$dir'"; then
                    dir_to_copy+=("$relative_dir")
                    echo "Added $dir to the list of directories to copy."
                else
                    echo "Directory does not exist on the remote machine. Please enter a valid directory path."
                fi
            fi
       fi     
       done
    
fi
echo

# Ask the user if they want to reinstall Flatpak applications
IFS= read -p "Do you want to reinstall Flatpak applications on the new machine? ([y]/n): " -r reinstall_answer
reinstall_answer=${reinstall_answer:-y}

# Ask the user if they want to copy the Flatpak app data over
if [[ "$reinstall_answer" =~ ^[yY] ]]; then
    IFS= read -p "Do you want to copy the Flatpak app data over, too? ([y]/n): " -r data_answer
    data_answer=${data_answer:-y}
fi

# Ask the user if they want to migrate Toolbx containers
if command -v toolbox &>/dev/null; then
    # Run toolbox list command and store the output
    toolbox_list_output=$(run_remote_command "toolbox list")
    # Extract container IDs and names using awk and store them in an array
    IFS=$'\n' read -r -d '' -a container_ids_and_names <<< "$(echo "$toolbox_list_output" | awk '/^CONTAINER ID/{flag=1; next} flag && /^[a-f0-9]+/{print $1 "\t" $2}')"
    # If there are any Toolbx containers on the origin machine, ask whether to migrate them
    if [ "${#container_ids_and_names[@]}" -gt 0 ]; then
    IFS= read -p "You seem to be using Toolbx, would you like to migrate its containers? ([y]/n): " -r toolbx_answer
    toolbx_answer=${toolbx_answer:-y}
    fi
fi

echo

IFS= read -p "Press enter to start the migration. It will take some time. You can leave the computer, have a coffee and wait until the migration is finished.
" -r

# Copy home directories over
copy_xdg_dir "DOCUMENTS" "$doc_answer"
copy_xdg_dir "VIDEOS" "$vid_answer"
copy_xdg_dir "PICTURES" "$pic_answer"
copy_xdg_dir "MUSIC" "$mus_answer"
copy_xdg_dir "DOWNLOAD" "$dwn_answer"

# Loop through directories picked by the user and copy them over
for copy_dir in "${dir_to_copy[@]}"; do
    sshpass -p "$password" rsync -chazP --chown="$USER:$USER" "$username@$origin_ip:$copy_dir/" "$HOME/$copy_dir"
done

# Reinstall flatpaks from the origin machine and copy over their data
if [[ "$reinstall_answer" =~ ^[yY] ]]; then
    # Capture the list of installed flatpaks on the origin machine
    installed_flatpaks_origin=$(run_remote_command "flatpak list --app --columns=application")
    # Parse the list using awk to skip the header and get the flatpak names
    flatpak_names_origin=$(echo "$installed_flatpaks_origin" | awk 'NR>1 {print}')
    # Capture the list of installed flatpaks on the destination machine
    installed_flatpaks_destination=$(flatpak list --app --columns=application)
    # Parse the list using awk to skip the header and get the flatpak names
    flatpak_names_destination=$(echo "$installed_flatpaks_destination" | awk 'NR>1 {print}')
    # Loop through each installed flatpak on the origin machine and install those that are not already installed on the destination machine
    while IFS= read -r flatpak_name <&3; do
        if [ -n "$flatpak_name" ]; then
            # Check if the flatpak is already installed on the destination machine
            if echo "$installed_flatpaks_destination" | grep -q "$flatpak_name"; then
                echo "Flatpak $flatpak_name is already installed. Skipping."
            else
                # Install the flatpak on the local machine
                flatpak install -y flathub "$flatpak_name"
            fi
        fi
    done 3<<< "$flatpak_names_origin"
    echo "Flatpak applications have been reinstalled on the new machine."
    # Copy flatpak app data in ~/.var/app/ over from the origin machine
    if [[ "$data_answer" =~ ^[yY] ]]; then
        echo "Now the flatpak app data will be copied over."
        mkdir -p "$HOME/.var/app"
        sshpass -p "$password" rsync -chazP --chown="$USER:$USER" "$username@$origin_ip:$user_home_origin/.var/app/" "$HOME/.var/app/"
    fi
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
