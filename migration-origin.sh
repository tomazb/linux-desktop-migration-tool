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

echo "This is a tool that helps with migration to a new computer."
(doc_answer=$(get_copy_decision "DOCUMENTS") ; echo "User's answer: $doc_answer") &

directory=$(xdg-user-dir "VIDEOS")
size_in_gb=$(get_directory_size "$directory")
echo "Copy over Videos? The size of the Videos folder is ${size_in_gb}GB? (y/n)"
read vid_answer
echo $vid_answer

# Ask the user if they want to reinstall Flatpak applications
read -p "Do you want to reinstall Flatpak applications on the new machine? (y/n): " answer

if [[ "$answer" == "y" || "$answer" == "Y" ]]; then
    # Perform the command to list installed Flatpak applications and save it to a file
    flatpak list --app --columns=application > installed_flatpaks.txt
    echo "List of installed Flatpaks saved to 'installed_flatpaks.txt'."
else
    echo "No action taken. Flatpak applications will not be reinstalled."
fi

echo -p "Enter the destination IP address:"
read dest_ip

echo -p "Enter the destination username:"
read username

echo -p -s "Enter the destination password:"
read password

sshpass -p "$password" ssh -o StrictHostKeyChecking=accept-new "$username@$dest_ip" 'xargs flatpak install -y flathub < flatpaks.txt'

