"""
This module provides API functions for interacting with the graph service.
"""
import hashlib
import os
import logging
import urllib

from azure.identity import InteractiveBrowserCredential
from msgraph import GraphServiceClient

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("process_log.log"),
        logging.StreamHandler()
    ]
)


class GraphAPI:
    """
    This class handles operations related to graph data processing and communication.
    """
    def __init__(self):
        self.credential = InteractiveBrowserCredential(
            client_id=os.getenv('CLIENT_ID'),
            tenant_id=os.getenv('TENANT_ID'),
        )
        self.scopes = ["User.Read", "Files.Read", "Files.Read.All"]
        self.client = GraphServiceClient(credentials=self.credential, scopes=self.scopes)

    @staticmethod
    def compute_hash(filename: str, hash_type='sha256') -> str:
        """
        Compute the hash of a file using the specified hash function.

        :param filename: The path to the file.
        :param hash_type: Type of hash to compute ('sha256' or 'sha1').
        :return: The computed hash as a hexadecimal string.
        """
        buf_size = 65536  # Read in 64kb chunks to avoid memory issues
        hasher = hashlib.new(hash_type)

        with open(filename, 'rb') as file:
            while chunk := file.read(buf_size):
                hasher.update(chunk)

        return hasher.hexdigest()

    async def search_file(self, file_name: str, file_path: str) -> list:
        """
        Search for a file in the graph service based on name and verify against local file size and hash.

        :param file_name: The name of the file to search.
        :param file_path: The full path of the local file.
        :return: A list of matched items.
        """
        # Safely encode the file_name for use in the URL
        encoded_file_name = urllib.parse.quote(file_name, safe='')
        raw_url = f"https://graph.microsoft.com/v1.0/me/drive/root/search(q='{encoded_file_name}')?select=name,id,size,file"

        item_list = await self.client.me.drive.with_url(raw_url).get()
        if not item_list.additional_data.get("value", []):
            logging.info(f"Skipped as couldn't find file: {file_path}")
            return []
        matched_items = []

        file_size = os.path.getsize(file_path)
        local_sha256 = self.compute_hash(file_path, 'sha256')
        local_sha1 = self.compute_hash(file_path, 'sha1')

        for item in item_list.additional_data.get("value", []):
            if item["size"] != file_size:
                logging.info(f"Skipped as size different: {file_path} -> API: {item['size']} Local: {file_size}")
                continue

            api_sha256 = item["file"]["hashes"].get("sha256Hash", "").lower()
            api_sha1 = item["file"]["hashes"].get("sha1Hash", "").lower()

            if api_sha256:
                if local_sha256 == api_sha256:
                    matched_items.append(item)
                else:
                    logging.info(f"Skipped as SHA256 hash was different: {file_path} -> Local: {local_sha256} vs. API: {api_sha256}")
            elif api_sha1:
                if local_sha1 == api_sha1:
                    matched_items.append(item)
                else:
                    logging.info(f"Skipped as SHA1 hash was different: {file_path} -> Local: {local_sha1} vs. API: {api_sha1}")
            else:
                logging.info(f"Skipped as hashes were absent: {file_path}")

        return matched_items
