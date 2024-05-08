import os
import hashlib
from azure.identity import InteractiveBrowserCredential
from msgraph import GraphServiceClient

# from pathlib import Path



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
        buff_size = 65536  # lets read stuff in 64kb chunks!

        sha1 = hashlib.sha1()

        with open(filename, 'rb') as f:
            while True:
                data = f.read(buff_size)
                if not data:
                    break
                sha1.update(data)
        return sha1.hexdigest()

    async def search_file(self, file_name: str, file_path: str) -> list:

        raw_url = "https://graph.microsoft.com/v1.0/me/drive/root/search(q='" 
        raw_url = raw_url + file_name + "')?select=name,id,size,file"
        item_list = await self.client.me.drive.with_url(raw_url).get()
        # item_list.additional_data["value"][0]["name"]
        matched_items = []
        for item in item_list.additional_data["value"]:
            if item["size"] == os.path.getsize(file_path):
                if item["file"]["hashes"]["sha256Hash"] != "":
                    if self.sha256sum(file_path).lower() == \
                        item["file"]["hashes"]["sha256Hash"].lower():
                        matched_items.append(item)
                elif item["file"]["hashes"]["sha1Hash"] != "":
                    if self.sha1sum(file_path).lower() == \
                        item["file"]["hashes"]["sha1Hash"].lower():
                        matched_items.append(item)
        return matched_items


# graph_api.py:49:77: C0303: Trailing whitespace (trailing-whitespace)
# graph_api.py:57:0: C0301: Line too long (105/100) (line-too-long)
# graph_api.py:60:0: C0301: Line too long (101/100) (line-too-long)
# graph_api.py:1:0: C0114: Missing module docstring (missing-module-docstring)
# graph_api.py:8:0: C0115: Missing class docstring (missing-class-docstring)
# graph_api.py:18:4: C0116: Missing function or method docstring (missing-function-docstring)
# graph_api.py:20:8: C0103: Variable name "BUF_SIZE" doesn't conform to snake_case naming style (invalid-name)
# graph_api.py:33:4: C0116: Missing function or method docstring (missing-function-docstring)
# graph_api.py:35:8: C0103: Variable name "BUF_SIZE" doesn't conform to snake_case naming style (invalid-name)
# graph_api.py:47:4: C0116: Missing function or method docstring (missing-function-docstring)

