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

get_copy_decision() {
    local directory="$1"
    # Get the directory path using xdg-user-dir
    local directory_path=$(run_remote_command "xdg-user-dir \"$directory\"")
    # Get the size of the directory
    local size_in_gb=$(get_directory_size "$directory_path")
    local directory_name=$(basename "$directory_path")
    # Ask the user if the directory should be included
    read -p "Copy over $directory_name? The size of the folder is ${size_in_gb}GB. (y/n): " answer

    # Return the user's answer
    echo "$answer"
}

copy_xdg_dir() {
    local directory="$1"
    local answer="$2"
    local directory_path=$(xdg-user-dir "$directory")
    local directory_name=$(basename "$directory_path")
    
    if [[ "$answer" == "y" || "$answer" == "Y" ]]; then
        # Create the local directory if it doesn't exist
        mkdir -p "$directory_path"
        
        # Copy the directory from remote to local using rsync
        run_remote_command "rsync -avz /home/$username/$directory_name $directory_path"
        echo "The $directory_name has been copied over." 
    fi
}

read -p "This is a tool that helps with migration to a new computer. It has several preconditions:

- Both computers need to be on the same local network. You will need to know the IP address of the origin computer. You can find it out in the network settings.
- The origin computer needs to have remote login via ssh enabled. You can enable it in Settings/Sharing.
- The destination computer is expected to be freshly installed with the user set up. Any data at the destination computer may be overriden.
- Already installed flatpaks will be reinstalled from Flathub.

Press Enter to continue or Ctrl+C to quit.

"

read -p "Enter the origin IP address: " origin_ip

read -p "Enter the origin username: " username

echo -n "Enter the user password: "
read -s password
echo

doc_answer=$(get_copy_decision "DOCUMENTS")

vid_answer=$(get_copy_decision "VIDEOS")

pic_answer=$(get_copy_decision "PICTURES")

mus_answer=$(get_copy_decision "MUSIC")

dwn_answer=$(get_copy_decision "DOWNLOAD")

echo

# Ask the user if they want to reinstall Flatpak applications
read -p "Do you want to reinstall Flatpak applications on the new machine? (y/n): " reinstall_answer

#Ask the user if they want to copy the Flatpak app data over
if [[ "$reinstall_answer" == "y" || "$reinstall_answer" == "Y" ]]; then
    read -p "Do you want to copy the Flatpak app data over, too? (y/n): " data_answer
fi

if [[ "$reinstall_answer" == "y" || "$reinstall_answer" == "Y" ]]; then
    # Perform the command to list installed Flatpak applications on the remote machine and save it to a file
    run_remote_command "flatpak list --app --columns=application" > installed_flatpaks.txt
    echo "List of installed Flatpaks saved to 'installed_flatpaks.txt'."
else
    echo "No action taken. Flatpak applications will not be reinstalled."
fi

echo

read -p "Press enter to start the migration. It will take some time. You can leave the computer, have a coffee and wait until the migration is finished.
"

#Copy home directories over
copy_xdg_dir "DOCUMENTS" "$doc_answer"
copy_xdg_dir "VIDEOS" "$vid_answer"
copy_xdg_dir "PICTURES" "$pic_answer"
copy_xdg_dir "MUSIC" "$mus_answer"
copy_xdg_dir "DOWNLOAD" "$dwn_answer"

if [[ "$reinstall_answer" == "y" || "$reinstall_answer" == "Y" ]]; then
    xargs flatpak install -y --reinstall flathub < installed_flatpaks.txt
    echo "Flatpak applications have been reinstalled on the new machine."
fi

if [[ "$data_answer" == "y" || "$reinstall_answer" == "Y" ]]; then
    # Copy flatpak app data in ~/.var/app/ over from the old machine
    echo "Now the flatpak app data will be copied over."
    run_remote_command "rsync -avz /home/$username/.var/app/ $HOME/.var/app"
fi

echo "
The migration is finished!"
