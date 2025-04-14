#!/bin/bash

#Copyright 2023 Jiri Eischmann
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/>. 

# Function to run a command remotely over SSH, based on the second parameter it can run as either normal or privileged user
run_remote_command() {
    local command_to_run="$1"
    local use_sudo="${2:-false}"

    if [[ "$use_sudo" == "true" ]]; then
        # SSH into the remote machine and execute the command with sudo
        sshpass -p "$password" ssh -o StrictHostKeyChecking=accept-new "$username@$origin_ip" "echo '$password' | sudo -S $command_to_run"
    else
        # SSH into the remote machine and execute the command without sudo
        sshpass -p "$password" ssh -o StrictHostKeyChecking=accept-new "$username@$origin_ip" "$command_to_run"
    fi
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

# Function to ask whether the XDG directory should be copied over
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

# Fuction to run commands locally using sudo
run_local_sudo() {
    local cmd="$1"
    echo "$local_password" | sudo -S --prompt='' bash -c "$cmd"
}

IFS= read -p "LINUX DESKTOP MIGRATION TOOL
This is a tool that helps with migration to a new computer. It has several preconditions:

- Both computers need to be on the same local network. You will need to know the IP address of the origin computer. You can find it out in the network settings.
- The origin computer needs to have remote login via ssh enabled. You can enable it in Settings/Sharing.
- The destination computer is expected to be freshly installed with the user set up. Any data at the destination computer may be overridden.

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
    if sshpass -p "$password" ssh -o StrictHostKeyChecking=accept-new -q "$username"@"$origin_ip" exit; then
        echo "The connection has been successfully established."
        break  # Break the loop if the connection is successful
    else
        echo "The connection with the origin computer could not be established. Please check the login information and make sure remote login is enabled on the origin computer.
        "
    fi
done

# Asking whether to copy files in home directories
IFS= read -p "Do you want to migrate files in home directories? ([y]/n): " -r copy_answer
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
            # Prevent migrating the .ssh directory because the script using ssh would just crash after the migration
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

# Ask the user if they want to migrate settings
IFS= read -p "Do you want to migrate desktop and app settings? ([y]/n) " -r settings_answer
settings_answer=${settings_answer:-y}       

# Check whether the user on the origin machine is priviledged in order to know whether the tool can do operations that require a priviledged access on both machines. If conditions are safisfied, ask whether to migrate network settings.

if run_remote_command "command -v nmcli &>/dev/null" "false" && command -v nmcli &>/dev/null; then
    remote_user_groups=$(run_remote_command "groups" "false")
    if [[ $remote_user_groups == *"sudo"* ]] || [[ $remote_user_groups == *"wheel"* ]]; then
        remote_is_privileged=true
    else
        remote_is_privileged=false
    fi

    local_user_groups=$(groups)
    if [[ $local_user_groups == *"sudo"* ]] || [[ $local_user_groups == *"wheel"* ]]; then
        local_is_privileged=true
    else
        local_is_privileged=false
    fi

    if [[ $remote_is_privileged == true ]] || [[ $local_is_privileged == true ]]; then
        IFS= read -p "Do you want to migrate network settings? ([y]/n) " -r network_answer
        network_answer=${network_answer:-y}
        if [[ $network_answer =~ ^[yY] ]]; then
            echo -n "This operation requires your local user password: " 
            IFS= read -rs local_password
        fi    
    else
        echo "The user on either the origin machine or the destination machine doesn't have rights required for the migration of network settings. Skipping."
    network_answer="n"
    fi
else
    echo "NetworkManager was not detected either on the origin or destination machine. Network settings won't be migrated."
fi

echo

# Ask the user if they want to migrate credentials and secrets
IFS= read -p "Do you want to migrate credentials and secrets (the keyring, ssh certificates and settings, PKI certificates)? ([y]/n) " -r secrets_answer
secrets_answer=${secrets_answer:-y}

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
        # Get the architecture of the local machine
        local_arch=$(arch)
        # Get the architecture of the remote machine
        remote_arch=$(run_remote_command "arch")
        # Check whether the hardware architecture is the same on both machines. If not, warn the user about it. Migration between different architectures is not disabled in case the architecture detection malfunctions or the user has a specific reason for the migration
        if [ "$local_arch" == "$remote_arch" ]; then 
            IFS= read -p "You seem to be using Toolbx, would you like to migrate its containers? ([y]/n): " -r toolbx_answer
            toolbx_answer=${toolbx_answer:-y}
        else
            IFS= read -p "You seem to be using Toolbx, but the destination machine has a different hardware architecture than the origin one, existing containers won't work. Unless you have a specific reason, you should not migrate them. Migrate them anyway? (y/[n]): " -r toolbx_answer
            toolbx_answer=${toolbx_answer:-n}
        fi     
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
            sshpsshpass -p "$password" rsync -chazP --chown="$USER:$USER" "$username@$origin_ip:$user_home_origin/.pki/" "$HOME/.pki/"ass -p "$password" rsync -chazP --remove-source-files --chown="$USER:$USER" --stats "$username@$origin_ip:$container_name.tar" .
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

# Migrate secrets and certificates
if [[ "$secrets_answer" =~ ^[yY] ]]; then
    # Copy over the directory with keyrings
    sshpass -p "$password" rsync -chazP --chown="$USER:$USER" "$username@$origin_ip:$user_home_origin/.local/share/keyrings/" "$HOME/.local/share/keyrings/"
    # Copy over the directory with pki certificates and nss database
    sshpass -p "$password" rsync -chazP --chown="$USER:$USER" "$username@$origin_ip:$user_home_origin/.pki/" "$HOME/.pki/"
    # Check whether the gpg tool is present on both machines and if it is, migrate gpg keys
    if ! command -v gpg &> /dev/null || ! run_remote_command "command -v gpg &> /dev/null"; then
        echo "GPG (GNU Privacy Guard) is not detected on one or both machines. Skipping GPG key migration."
    else
        # Export GPG keys on the origin machine
        run_remote_command "gpg --armor --export > $user_home_origin/gpg_keys.asc"
        # Copy the file with exported keys over
        sshpass -p "$password" rsync -chazP --chown="$USER:$USER" "$username@$origin_ip:$user_home_origin/gpg_keys.asc" "$HOME/"
        # Import the keys from the file
        gpg --import "$HOME"/gpg_keys.asc
        # Delete the file with keys on both machines
        run_remote_command "rm $user_home_origin/gpg_keys.asc"
        rm "$HOME"/gpg_keys.asc
    fi
    
    # Migrate GNOME Online Accounts
    if run_remote_command "test -e '$user_home_origin/.config/goa-1.0/accounts.conf'"; then
        sshpass -p "$password" rsync -chazP --chown="$USER:$USER" "$username@$origin_ip:$user_home_origin/.config/goa-1.0/accounts.conf" "$HOME/.config/goa-1.0/"
    else
        echo "GNOME Online Accounts don't seem to be set up on the origin machine. Skipping."
    fi    

    # Migrate ssh certificates and settings
    # Make a temporary .ssh dir
    mdir -p "$HOME"/.ssh-migration
    # Copy the .ssh dir over to the temporary dir to avoid an ssh connection crash
    sshpass -p "$password" rsync -chazP --chown="$USER:$USER" "$username@$origin_ip:$user_home_origin/.ssh/" "$HOME/.ssh-migration/"
    # Copy files from the temporary dir to ~/.ssh once the connection is closed
    cp -a "$HOME"/.ssh-migration/* "$HOME"/.ssh/
    # Delete the temporary dir
    rm -r "$HOME"/.ssh-migration
    echo "GNOME Online Accounts, secrets and certificates migrated.
    "
fi

# Migrate settings
if [[ "$settings_answer" =~ ^[yY] ]]; then
    # Export Dconf settings on the origin machine and save to a variable
    dconf_output=$(run_remote_command 'dconf dump /')
    
    # Check if the command was successful
    if [[ $? -eq 0 ]]; then
        echo "Desktop settings from the origin machine loaded successfully."
        
        # Import Dconf settings on the destination machine directly
        echo "$dconf_output" | dconf load -f /
        
        echo "Settings imported on the destination machine successfully.
        "
        
    else
        echo "Loading desktop settings from the origin computer failed."
    fi
    # Migrate the desktop background
    # Read the picture-uri from the remote machine
    remote_background_uri=$(run_remote_command "dconf read /org/gnome/desktop/background/picture-uri")

    # Remove quotes from the URI
    remote_background_uri=$(echo "$remote_background_uri" | tr -d "'")

    # Check if the URI is a file URI
    if [[ "$remote_background_uri" == file://* ]]; then
        # Extract the file path from the URI
        remote_file_path="${remote_background_uri#file://}"

        # Check if the file exists on the local machine
        if [[ ! -f "$remote_file_path" ]]; then
            # Copy the file from the remote machine to the local machine
            sshpass -p "$password" rsync -chazP --chown="$USER:$USER" "$username@$origin_ip:$remote_file_path" "$HOME/.config/background"
            if [[ $? -eq 0 ]]; then
                # Set the dconf key to the copied background file in URI format
                dconf write /org/gnome/desktop/background/picture-uri "'file://"$HOME"/.config/background'"
                dconf write /org/gnome/desktop/background/picture-uri-dark "'file://"$HOME"/.config/background'"
                dconf write /org/gnome/desktop/screensaver/picture-uri "'file://"$HOME"/.config/background'"
            fi  
        else
            echo "The background is already present on the destination machine."
        fi
    else
        echo "The picture-uri is not a valid file URI."
    fi
fi

# Migrate network settings
if [[ "$network_answer" =~ ^[yY] ]]; then
    mapfile -t network_files < <(
        run_remote_command "find /etc/NetworkManager/system-connections/ -type f -printf '%f\\n'" "true"
    )
    for file in "${network_files[@]}"; do
        echo "Processsing: $file"
        
        # Fetching content
        encoded_content=$(run_remote_command "base64 -w0 \"/etc/NetworkManager/system-connections/${file}\"" "true")
        
        # Reconstructing the config files on the destination machine
        run_local_sudo "base64 -d <<< '${encoded_content}' > /etc/NetworkManager/system-connections/${file@Q}"
        run_local_sudo "chmod 600 /etc/NetworkManager/system-connections/${file@Q}"
        run_local_sudo "chown root:root /etc/NetworkManager/system-connections/${file@Q}"
        
        # Removing the .nmconnection suffix
        connection_name="${file%.nmconnection}"
        
        # Modify permissions to set the current local user
        run_local_sudo "nmcli connection modify '${connection_name}' connection.permissions 'user:$(whoami)'"
        
        # If the connection is tied to an interface that is specific to the origin machine, remove it, the connection will then map to any interface on the destination machine       
        run_local_sudo "nmcli connection modify ${connection_name} connection.interface-name ''"
    done
    
    run_local_sudo "systemctl restart NetworkManager"
    echo "Network settings successfully migrated."
fi

echo "
The migration is finished! Log out and in for all changes to take effect."
