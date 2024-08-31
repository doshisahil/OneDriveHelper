import asyncio
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import os
import graph_api
import tkinter as tk
from tkinter import messagebox
import glob
from pathlib import Path


async def main(local_drive_path):
    if not local_drive_path:
        messagebox.showerror("Error", "Please enter local drive path.")
        exit()

    if not os.path.exists(local_drive_path):
        messagebox.showerror("Error", "Local drive path does not exist.")
        exit()
    graph_api_helper = graph_api.GraphAPI()

    # Get list of files in the local drive
    image_extensions = ['*.jpg', '*.jpeg', '*.png', '*.mp4', '*.mpg', "*.mov", "*.mp4", "*.MTS", "*.avi", "*.heif",
                        "*.heifs", "*.heic", "*.heics", "*.avci", "*.avcs", "*.hif"]

    for ext in image_extensions:
        pathlist = Path(local_drive_path).rglob("**/" + ext)
        for path in pathlist:
            path_in_str = str(path)
            matched_files = await graph_api_helper.search_file(str(path.name), path_in_str)
            if len(matched_files) != 0:
                os.remove(path_in_str)
                print("Deleting " + path_in_str)
            else:
                print("Skipped " + path_in_str)

        pathlist = Path(local_drive_path).rglob("**/" + ext.upper())
        for path in pathlist:
            path_in_str = str(path)
            matched_files = await graph_api_helper.search_file(str(path.name), path_in_str)
            if len(matched_files) != 0:
                os.remove(path_in_str)
                print("Deleting " + path_in_str)
            else:
                print("Skipped " + path_in_str)


local_drive_path = input("Enter Drive location: ")
asyncio.run(main(local_drive_path))
