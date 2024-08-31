from azure.identity import InteractiveBrowserCredential
from msgraph import GraphServiceClient
import os
from pathlib import Path
import hashlib


class GraphAPI:
    def __init__(self):
        credential = InteractiveBrowserCredential(
            client_id=os.getenv('client_id'),
            tenant_id=os.getenv('tenant_id'),
        )
        scopes = ["User.Read", "Files.Read", "Files.Read.All"]
        self.client = GraphServiceClient(credentials=credential, scopes=scopes, )

    @staticmethod
    def sha256sum(filename: str):
        # BUF_SIZE is totally arbitrary, change for your app!
        BUF_SIZE = 65536  # lets read stuff in 64kb chunks!

        sha2 = hashlib.sha256()

        with open(filename, 'rb') as f:
            while True:
                data = f.read(BUF_SIZE)
                if not data:
                    break
                sha2.update(data)
        return sha2.hexdigest()

    @staticmethod
    def sha1sum(filename: str):
        # BUF_SIZE is totally arbitrary, change for your app!
        BUF_SIZE = 65536  # lets read stuff in 64kb chunks!

        sha1 = hashlib.sha1()

        with open(filename, 'rb') as f:
            while True:
                data = f.read(BUF_SIZE)
                if not data:
                    break
                sha1.update(data)
        return sha1.hexdigest()

    async def search_file(self, file_name: str, file_path: str) -> list:
        raw_url = "https://graph.microsoft.com/v1.0/me/drive/root/search(q='" + file_name + "')?select=name,id,size,file"
        item_list = await self.client.me.drive.with_url(raw_url).get()
        # item_list.additional_data["value"][0]["name"]
        matched_items = []
        for item in item_list.additional_data["value"]:
            if item["size"] == os.path.getsize(file_path):
                if item["file"]["hashes"]["sha256Hash"] != "":
                    if self.sha256sum(file_path).lower() == item["file"]["hashes"]["sha256Hash"].lower():
                        matched_items.append(item)
                    else:
                        print("1)Skipped as hash was different :" + file_path + "->" + self.sha256sum(file_path).lower() + ":" +
                              item["file"]["hashes"]["sha256Hash"].lower())
                elif item["file"]["hashes"]["sha1Hash"] != "":
                    if self.sha1sum(file_path).lower() == item["file"]["hashes"]["sha1Hash"].lower():
                        matched_items.append(item)
                    else:
                        print("2)Skipped as hash was different :" + file_path + "->" + self.sha1sum(file_path).lower() + ":" +
                              item["file"]["hashes"]["sha1Hash"].lower())
                else:
                    print("3)Skipped as hash was absent :" + file_path)
            else:
                print("4)Skipped as size different :" + file_path+"->"+str(item["size"])+":"+str(os.path.getsize(file_path)))
        return matched_items
