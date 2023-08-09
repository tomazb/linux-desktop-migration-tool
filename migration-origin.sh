#!/bin/bash

# Function to get the size of a directory
get_directory_size() {
    local directory="$1"

    # Check if the directory exists
    if [ -d "$directory" ]; then
        # Calculate the size of the directory in bytes using du command
        local dir_size=$(du -sb "$directory" | cut -f1)
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
    local directory_path=$(xdg-user-dir "$directory")
    # Get the size of the directory
    local size_in_gb=$(get_directory_size "$directory_path")
    # Ask the user if the directory should be included
    read -p "Copy over $directory? The size of the $directory folder is ${size_in_gb}GB. Do you want to include it? (y/n): " answer

    # Return the user's answer
    echo "$answer"
}

copy_xdg_dir() {
    local directory="$1"
    local answer="$2"
    local dest_ip="$3"
    local username="$4"
    local password="$5"
    local directory_path=$(xdg-user-dir "$directory")
    local directory_name=$(basename "$directory_path")
    
    if [[ "$answer" == "y" ]]; then
        #sshpass -p "$password" scp -r -o StrictHostKeyChecking=no "$directory_path/" "$username@$dest_ip:$/home/$username/$directory_name"
        sshpass -p "$password" sftp -o StrictHostKeyChecking=no "$username@$dest_ip" <<EOF
        cd /home/$username/$directory_name
        lcd "$directory_path"
        mput -r .
EOF
        echo "The $directory_path has been copied over." 
    fi
}

read -p "This is a tool that helps with migration to a new computer. It has several preconditions:

- Both computers need to be on the same local network. You will need to know the IP address of the destination computer. You can find it out in the network settings.
- The destination computer needs to have remote login via ssh enabled. You can enable it in Settings/Sharing.
- The destination computer is expected to be freshly installed with the user set up. Any data at the destination computer may be overriden.
- Already installed flatpaks will be reinstalled from Flathub.

Press Enter to continue or Ctrl+C to quit.

"

doc_answer=$(get_copy_decision "DOCUMENTS")

vid_answer=$(get_copy_decision "VIDEOS")

img_answer=$(get_copy_decision "IMAGES")

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
    # Perform the command to list installed Flatpak applications and save it to a file
    flatpak list --app --columns=application > installed_flatpaks.txt
    echo "List of installed Flatpaks saved to 'installed_flatpaks.txt'."
else
    echo "No action taken. Flatpak applications will not be reinstalled."
fi

read -p "Enter the destination IP address: " dest_ip

read -p "Enter the destination username: " username

echo -n "Enter the username password: "
read -s password
echo

read -p "Press enter to start the migration. It will take some time. You can leave the computer, have a coffee and wait until the migration is finished.
"

read -p "Press to continue"

#Copy home directories over
copy_xdg_dir "DOCUMENTS" "$doc_answer" "$dest_ip" "$username" "$password"
copy_xdg_dir "VIDEOS" "$vid_answer" "$dest_ip" "$username" "$password"
copy_xdg_dir "IMAGES" "$img_answer" "$dest_ip" "$username" "$password"
copy_xdg_dir "MUSIC" "$mus_answer" "$dest_ip" "$username" "$password"
copy_xdg_dir "DOWNLOAD" "$dwn_answer" "$dest_ip" "$username" "$password"

if [[ "$reinstall_answer" == "y" || "$reinstall_answer" == "Y" ]]; then
    # Copy the file with the list of flatpaks to reinstall over to the new machine
    sshpass -p "$password" scp -r -o StrictHostKeyChecking=accept-new "./installed_flatpaks.txt" "$username@$dest_ip:/home/$username/"
    echo "The list of flatpaks to reinstall has been copied over to the new machine, now the reinstallation will start."
fi

if [[ "$reinstall_answer" == "y" || "$reinstall_answer" == "Y" ]]; then
    sshpass -p "$password" ssh -o StrictHostKeyChecking=accept-new "$username@$dest_ip" 'xargs flatpak install -y --reinstall flathub < installed_flatpaks.txt'
    echo "Flatpak applications have been reinstalled on the new machine."
fi

if [[ "$data_answer" == "y" || "$reinstall_answer" == "Y" ]]; then
    # Copy flatpak app data in ~/.var/app/ over to the new machine
    echo "Now the flatpak app data will be copied over."
    sshpass -p "$password" sftp -o StrictHostKeyChecking=no "$username@$dest_ip" <<EOF
        cd /home/$username/.var/app/
        lcd "$HOME"/.var/app
        mput -r .
EOF
fi

echo "
The migration is finished!"
