'''
    @author neucrack
    @date 2020.10.10
    @update 2022.3.12 fix download range and optimize for short file
    @license MIT
'''

import threading
import requests
import os
import sys
import time
# from progress.bar import Bar

from threading import Lock
import hashlib
import tarfile
import zipfile
import lzma
import json

from rich import print
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm

sdk_path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

lock = Lock()

def bytes2human(n_bytes):
    #return B if n_bytes < 1024, KiB if n_bytes < 1024*1024, MiB if < 1024*1024*1024
    if n_bytes < 1024:
        return "{} B".format(n_bytes)
    elif n_bytes < 1024*1024:
        return "{:.2f} KiB".format(n_bytes/1024)
    elif n_bytes < 1024*1024*1024:
        return "{:.2f} MiB".format(n_bytes/1024/1024)
    else:
        return "{:.2f} GiB".format(n_bytes/1024/1024/1024)

def unzip_file(zip_file, dst_dir):
    ext = os.path.splitext(zip_file)[1]
    if ext == ".zip":
        with zipfile.ZipFile(zip_file, 'r') as z:
            z.extractall(dst_dir)
    elif ext == ".gz" and zip_file.endswith(".tar.gz"):
        with tarfile.open(zip_file, 'r:gz') as t:
            t.extractall(dst_dir)
    elif ext == ".xz" and zip_file.endswith(".tar.xz"):
        with tarfile.open(zip_file, 'r:xz') as t:
            t.extractall(dst_dir)
    elif ext == ".xz":
        with lzma.open(zip_file, 'r') as f:
            with open(os.path.join(dst_dir, os.path.basename(zip_file)[:-3]), 'wb') as w:
                w.write(f.read())
    else:
        print("Error: file %s unsupported file type %s" % (zip_file, ext))
        sys.exit(1)

def check_sha256sum(file, sum):
    with open(file, "rb") as f:
        sha256sum=hashlib.sha256(f.read()).hexdigest()
    return sha256sum == sum, sha256sum

class Downloader:
    def __init__(self, url, save_path, max_thread_num=8, force_write=False, headers={}):
        self.url = url
        self.save_path = os.path.abspath(os.path.expanduser(save_path))
        self.max_thread_num = max_thread_num
        self.headers = headers
        self.force_write = force_write
        self.setup_download()

    def setup_download(self):
        os.makedirs(os.path.dirname(self.save_path), exist_ok=True)
        self.temp_path = self.save_path + ".temp"
        with requests.head(self.url, headers=self.headers) as res:
            if res.status_code == 302:
                self.url = res.headers['Location']
                return self.setup_download()
            if res.status_code != 200:
                raise Exception("Failed to prepare download. Status code: {}, Content: {}".format(res.status_code, res.text))
            self.total = int(res.headers.get('Content-Length', 0))

    def download_chunk(self, start, end):
        headers = self.headers.copy()
        headers['Range'] = 'bytes={}-{}'.format(start, end)
        response = requests.get(self.url, headers=headers)
        if response.status_code not in [200, 206]:
            raise Exception("Failed to download chunk. Status code: {}".format(response.status_code))
        return response.content

    def combine_files(self):
        with open(self.temp_path, 'wb') as fw:
            for i in range(self.max_thread_num):
                part_path = f"{self.temp_path}.part{i}"
                with open(part_path, 'rb') as fr:
                    fw.write(fr.read())
                os.remove(part_path)

    def start(self):
        if os.path.exists(self.save_path) and not self.force_write:
            raise Exception("File already exists.")
        
        chunk_size = self.total // self.max_thread_num
        futures = []
        with ThreadPoolExecutor(max_workers=self.max_thread_num) as executor, tqdm(total=self.total, unit='B', unit_scale=True, desc="Downloading") as progress:
            for i in range(self.max_thread_num):
                start = i * chunk_size
                end = start + chunk_size - 1 if i != self.max_thread_num - 1 else self.total
                futures.append(executor.submit(self.download_chunk, start, end))
        
            # Save chunks to temp files and update progress bar
            for i, future in enumerate(futures):
                part_path = f"{self.temp_path}.part{i}"
                content = future.result()
                with open(part_path, 'wb') as f:
                    f.write(content)
                    progress.update(len(content))

        self.combine_files()
        os.rename(self.temp_path, self.save_path)
        print("Download completed.")

def check_download_items(items):
    keys = ["url", "sha256sum", "path"]
    for item in items:
        for key in keys:
            if key not in item:
                print("-- Error: {} not found in download item: {}".format(key, item))
                sys.exit(1)
        if not item["url"]:
            print("-- Error: url not found in download item: {}".format(item))
            sys.exit(1)
        if not item["filename"]:
            item["filename"] = os.path.basename(item["url"])
        if not item["path"]:
            print("-- Error: path not found in download item: {}, for example, toolchanin can be 'toolchains/board_name'".format(item))
            sys.exit(1)
        if not "urls" in item:
            item["urls"] = []
        if not "sites" in item:
            item["sites"] = []
        if not "extract" in item:
            item["extract"] = True
        if not item["sha256sum"]:
            raise Exception("\n--!! [WARNING] sha256sum not found in download item: {}\n".format(item))

def download_extract_files(items):
    '''
        @items = [
            {
                "url": "http://****",
                "urls":[], # backup urls
                "sites":[], # backup sites, user can manually download
                "sha256sum": "****",
                "filename": "****",
                "path": "toolchains/m2dock", # will download to sdk_path/dl/pkgs/path/filename
                                             # and extract package to sdk_path/dl/extracted/path/
            }
    '''
    check_download_items(items)
    # show items
    info_path = os.path.join(sdk_path, "dl", "pkgs_info.json")
    print("\n-------------------------------------------------------------------")
    print("-- All {} files info need to be downloaded saved to\n   {}".format(len(items), info_path))
    for i, item in enumerate(items):
        item["pkg_path"] = os.path.join(sdk_path, "dl", "pkgs", item["path"], item["filename"])
    print("-------------------------------------------------------------------\n")
    os.makedirs(os.path.dirname(info_path), exist_ok=True)
    with open(info_path, "w") as f:
        json.dump(items, f, indent=4)

    for item in items:
        pkg_path = item["pkg_path"]
        extract_dir = os.path.join(sdk_path, "dl", "extracted", item["path"])
        if not os.path.exists(pkg_path):
            print("\n-------------------------------------------------------------------")
            print("-- Downloading {} from:\n   {},\n   save to: {}\n   you can also download it manually and save to this position{}{}".format(
                item["filename"], item["url"], pkg_path, 
                "\n   other urls: {}".format(item["urls"]) if item["urls"] else "",
                "\n   sites: {}".format(item["sites"]) if item["sites"] else ""))
            print("-------------------------------------------------------------------\n")
            headers = {
                # "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
            }
            if "user-agent" in item:
                headers["User-Agent"] = item["user-agent"]
            d = Downloader(item["url"], pkg_path, max_thread_num = 8, force_write=True, headers=headers)
            d.start()
            # check sha256sum
            if "sha256sum" in item and item["sha256sum"]:
                ok, sha256sum = check_sha256sum(pkg_path, item["sha256sum"])
                if not ok:
                    print("-- Error: file {} sha256sum check failed, shoule be {}, but files's sha256sum is {}.\n   Please download this file manually".format(pkg_path, item["sha256sum"], sha256sum))
                    sys.exit(1)
        # extract_dir not empty means already extracted, continue
        need_extract = False
        if "extract" in item and not item["extract"]:
            continue
        renamed_files = []
        if "rename" in item:
            for _from, _to in item["rename"].items():
                from_path = os.path.join(extract_dir, _from)
                to_path = os.path.join(extract_dir, _to)
                renamed_files.append((from_path, to_path))
        if os.path.exists(extract_dir):
            files = os.listdir(extract_dir)
            files_final = []
            for f in files:
                if f.endswith(".extracting"):
                    need_extract = True
                    break
                files_final.append(f)
            for from_path, to_path in renamed_files:
                if not os.path.exists(to_path):
                    need_extract = True
            for file in item.get("check_files", []):
                path = os.path.join(extract_dir, file)
                if not os.path.exists(path):
                    need_extract = True
                    break
            if not need_extract:
                need_extract = len(files_final) == 0
            if not need_extract:
                print("-- {} already extracted, skip".format(os.path.join("dl", "pkgs", item["path"], item["filename"])))
                continue
        # extract
        print("-- Extracting {} to {}".format(pkg_path, extract_dir))
        # write unzip tmp file to extract_dir/filename.extracting
        flag_file = os.path.join(extract_dir, item["filename"] + ".extracting")
        os.makedirs(os.path.dirname(flag_file), exist_ok=True)
        with open(flag_file, "wb"):
            pass
        unzip_file(pkg_path, extract_dir)
        for from_path, to_path in renamed_files:
            if not os.path.exists(from_path):
                raise Exception("rename file failed, {} not found. (to {})".format(from_path, to_path))
            os.rename(from_path, to_path)
        os.remove(flag_file)

if __name__ == "__main__":
    items_str = sys.argv[1]
    if os.path.exists(items_str):
        with open(items_str, "r") as f:
            items_str = f.read()
    else:
        items_str = ";".join(sys.argv[1:])
    items_str = items_str.replace("'", '"')
    items = items_str.split(";")
    for i, item in enumerate(items):
        try:
            items[i] = json.loads(item)
        except Exception as e:
            print("-- Error: parse json failed, content: {}".format(item))
            raise e
    download_extract_files(items)
